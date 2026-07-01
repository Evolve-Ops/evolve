# spec: app-usage advisor generator — 2026-06-03

Status: draft.

## Background

The recommendations-rework spec
([docs/spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md))
ships a new **Improvements** page (Phase 2C, [#2027](https://github.com/evolve-ops/evolve/pull/2027))
under the Improve sidebar bucket. That page is empty by design until
this generator lands and starts emitting `surface: improvement`
proposals. Phase 3 fills it.

The original operator complaint that started the rework:

> I was especially hoping for proposals about how the apps could be
> expanded to deliver better on the objective and the usage of each
> bot (looking at how a task management system is being used and
> proposing some helpful new features).

The existing generator portfolio reads config files and baselines.
None of them look at **how an app is actually being used vs. what
the app's manifest claims it does**, and propose product changes
that would close the gap. This spec covers the generator that does.

## Principle

> Read the app's stated objective (success criteria + scope_includes
> from the manifest). Read what the app is actually being used for
> (invocation patterns, channels, time-of-day, success/failure mix).
> Propose 1–2 concrete features or scope changes that would make the
> app deliver better on its stated objective, grounded in the observed
> use.

The generator is a **per-app weekly run**. It is opinionated and
sparse — at most 1–2 proposals per app per week. If the observation
window is too thin (< 5 invocations) it goes silent.

## Generator identity

```yaml
id: app_usage_advisor
schema_version: 1
type: optimizer
dimension: capabilities
bucket: extend
surface: improvement   # routes to the Improvements page (Phase 2C)
cadence: weekly
purpose: >
  Read each installed app's manifest and the bot's observed usage
  of it over the last 7 days. Propose 1-2 concrete additions that
  would close the gap between what the app claims to do and what
  it's actually being asked to do.
resolves_when_silent: false  # insight-style; not a sensor
```

## Input data

Per-app, per-bot, per-week:

| Source | Field used | Where it lives |
|---|---|---|
| App manifest | `identity.scope_includes`, `identity.scope_excludes`, `success_criteria.observable_outcomes`, `purpose` | `/Users/<bot>/.openclaw/workspace/apps/<app>/manifest.yaml` (read via existing `app_manifest_reader`) |
| Invocation log | Command name, args (redacted), timestamp, channel kind, trigger kind, success/failure | OpenClaw turn logs filtered by `app_id` from existing `cost_event` JSONL |
| Session context | trigger_kind, channel, user_message (first 200 chars), outcome status | Same JSONL |
| Failure mode | Did the bot fall back to "I can't help with that"? Did the user re-ask? | Inferred from short-loop pattern over consecutive sessions |
| Previous proposals | Recent advisor outputs for this app | `iter_proposals(generator_id="app_usage_advisor", bot_id=bot, ...)` for dedup |

All read-only inputs. No new instrumentation required for v1.

## Observation window

7 days, ending at run time. Skipped if:

- Fewer than **5 invocations** of the app in the window (insufficient signal).
- App was installed less than **14 days ago** (still bedding in).
- The advisor already proposed for this app within the last **30 days** and the operator hasn't acted on it (avoid pestering).

## LLM prompt shape

System prompt:

> You are evaluating whether the app `{app_name}` on bot `{bot_id}`
> is delivering on its stated objective for this user. You see the
> app's manifest (what it claims to do) and a one-week sample of how
> it's actually being invoked. Your job is to propose 1–2 concrete
> additions or scope changes that would close the gap between intent
> and observed use. Be specific. Cite the observations that motivate
> each suggestion. Skip if the app is delivering and there's nothing
> meaningful to add.

User prompt:

```
=== App manifest ===
{manifest.identity.purpose}
Scope includes: {scope_includes}
Scope excludes: {scope_excludes}
Success criteria:
{success_criteria.observable_outcomes | bullet-formatted}

=== Observed usage, last 7 days ({N_invocations} invocations) ===
{usage_summary}

Most common command patterns (top 5):
{top_commands}

Channels: {channel_breakdown}
Time-of-day: {tod_histogram}
Failure / fallback rate: {failure_rate}%
{if recurring_failures: "Recurring failure pattern: ..."}

=== Previously proposed (last 60 days) ===
{prior_proposals_summary or "none"}
```

Output schema: structured tool call (`structured_output_propose`) with

```json
{
  "should_emit": true|false,
  "rationale_if_silent": "...",        // when should_emit=false
  "summary": "1-line user-facing summary",
  "observed_pattern": "1-line observation",
  "gap_or_opportunity": "what's missing",
  "proposed_changes": [
    {
      "title": "short title",
      "description": "what to add / change",
      "manifest_field": "which manifest section it'd live in",
      "expected_impact": "what this would do for the user"
    }
  ],
  "confidence": 0.0..1.0
}
```

Skip emission when `should_emit` is false OR `confidence < 0.5`.

## Output: Proposal shape

For each emission:

| Field | Value |
|---|---|
| `generator_id` | `app_usage_advisor` |
| `bot_id` | the bot running the app |
| `urgency` | `improvement` |
| `dimension` | `capabilities` |
| `approval_audience` | `pod_operator` |
| `action` | `Investigation` (operator decides; not auto-applied) |
| `admin_surface_summary` | `f"{app_name} · {bot_id}: {summary[:80]}"` |
| `problem` | The full `summary` + `observed_pattern` + `gap_or_opportunity` paragraph |
| `provenance.signals` | `{ "app_id": ..., "observations": {...}, "llm_confidence": ..., "proposed_changes": [...] }` |

The Investigation context body carries the full prompt response, the
observation summary, and the list of proposed changes — operator
clicks "Take this on" to start an implementation session.

## Cost

Conservative per-run budget:

- ~5K input tokens (manifest + 1 week of summary + prior proposals)
- ~1K output tokens
- Single LLM call per app
- Workhorse-tier model (Sonnet-4.5 or equivalent)

At current rates (~$0.003/K input, ~$0.015/K output):
- ~$0.015 + ~$0.015 = **~$0.03 per app per week**
- 10-app pod = **~$0.30/week** = **~$1.20/month**

That's well within the existing per-bot budget caps. If a pod runs
hot on apps, the per-bot cost still scales linearly and remains
small relative to user-turn cost.

Skipped runs (insufficient observations / cool-down) cost $0.

## Cadence and triggering

- **Weekly per app**, fired by the existing `generator_runner` daily
  sweep checking `cadence: weekly`.
- Optionally: subscribe to `app_install` Signal — when a new app is
  installed and crosses the 14-day bedding-in threshold, the advisor
  runs once on it. Defer to v1.1 if not needed.

## Quality controls

1. **Operator decline suppression** — if the operator has declined N=2
   recent proposals from this advisor for the same app, go silent on
   that (app, bot) for 30 days. Same `operator_already_declined`
   helper as `bloat_investigator`.
2. **Signature dedup** — proposal signature is
   `app_usage_advisor:{app_id}:{proposed_changes_hash}`. Don't re-emit
   the same shape within the window.
3. **Confidence floor** — `should_emit=true && confidence >= 0.5` to emit.
4. **Track-record gating** — standard `GeneratorRecord.status` pattern.
   If `verified_failure_rate > 0.5` over the last 10 proposals, pause
   the generator.

## Privacy + safety

1. The advisor reads transcript-level data (command args, channel,
   first 200 chars of user message). All of this is already inside
   the bot's session log; nothing leaves the bot.
2. The advisor's LLM call uses the bot's own LLM credentials — per
   the existing pattern (memory: `feedback_per_bot_inference`).
3. The advisor must NOT propose features that:
   - Change the app's intended audience (e.g., make a personal app
     multi-user)
   - Add paid integrations (Stripe, third-party APIs that bill)
   - Capture data the operator hasn't approved
4. Hard-coded invariant in the charter:
   ```yaml
   invariants:
     - id: never_proposes_paid_integration
       description: Proposed changes must not require new paid services
       check_kind: custom
       params:
         deny_patterns: [stripe, billing, paid, subscription, credit_card]
   ```

## Out of scope for v1

- **Auto-applying changes** — the action is always `Investigation`.
  v1 hands the proposed-changes list to the operator; the operator
  decides whether to implement.
- **Cross-app suggestions** — the advisor runs per-app. A feature
  that spans apps (e.g., "task-manager and morning-brief should
  coordinate") needs cross-app context the advisor doesn't have in
  v1.
- **Backwards compatibility with old apps** — only apps with a valid
  manifest (`identity.scope_includes` + `success_criteria` present)
  are evaluated.

## Implementation outline

1. **`packages/analyzer/generators/app_usage_advisor/charter.yaml`** —
   the identity block above.
2. **`packages/analyzer/generators/app_usage_advisor/observe.py`** —
   - Iterate apps installed on the target bot
   - For each, build the observation window via the existing
     cost_event JSONL reader filtered by `app_id`
   - Skip if window thin / cool-down active
   - Build the prompt, call the bot's LLM via the existing
     `provider_router`
   - Validate output against the schema
   - Emit `Proposal` if `should_emit && confidence >= 0.5`
3. **`packages/analyzer/generators/app_usage_advisor/tests/`** —
   - Unit: skip when window thin, skip when cool-down active,
     dedup by signature, confidence floor honored, decline-
     suppression honored
   - Synthetic LLM fixtures for the happy path
   - Schema validation on the structured output

## Open questions

1. **Should the advisor's prompt include the app's recent
   proposals from other generators?** The risk is double-counting
   the operator's recent declines. The cleanest cut: keep the
   advisor's view limited to its own previous outputs + the bot's
   usage pattern.

2. **Should there be a "thumbs up" / "thumbs down" loop on
   proposals to train the next run's prompt?** Out of scope for v1
   (no calibration infrastructure assumed); deferred to v1.x if the
   advisor's hit rate is too low.

3. **How wide is the "channel kind" categorization?** Slack/Telegram/
   Discord are explicit; internal heartbeats are common but probably
   not useful to surface in the prompt. Default: filter heartbeats
   out of the user-facing observations, but show their share so the
   operator sees the automation-vs-user mix.

## Migration

Phased:

- **Phase 3A** — Charter + observe.py + tests. Generator ships
  paused-by-default in the registry so the test pod operator can
  unpause when ready (avoids surprise emissions during deploy).
- **Phase 3B** — Operator unpauses. First weekly run on the test
  pod. Calibrate the prompt and confidence floor based on output
  quality.
- **Phase 3C** — Document the advisor on the Improvements page's
  empty-state copy so the operator knows it's the source of
  improvements.

## Related

- [spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md)
  §"Phase 3" — Improvements page rendering surface
- `feedback_per_bot_inference` (memory) — LLM inference runs siloed
  per-bot with the bot's own credentials
- `project_app_framework_differentiator` (memory) — applications-as-
  contracts framing the advisor reinforces
- `spec-app-audit-2026-05-16.md` — sibling per-app generator (audit-
  shaped; this one is opportunity-shaped)
