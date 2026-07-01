# spec: agent freelance bypass of app controls — 2026-06-05

Status: Phase 1 in progress (2026-06-05).

## Background

The 2026-06-05 live test of atlas-on-demand-research in the OC
Interest Telegram group exposed a class gap. The bot received an
@-mention, followed AGENTS.md guidance, tried to invoke
``python3 scripts/atlas_research.py ask --query "…" …``, and OC's
exec preflight ([openclaw#87371](https://github.com/openclaw/openclaw/issues/87371))
rejected the command as "complex interpreter invocation." The
agent fell back to its general tools (Brave Search + native LLM)
and **answered freelance** — bypassing the script's controls
entirely.

[PR #2192](https://github.com/evolve-ops/evolve/pull/2192) added a
JSON-request-file invocation mode that passes OC preflight, plus
a per-app AGENTS.md instruction "do NOT freelance with general
tools if the script fails." That closed the *symptom* for two
atlas apps. This spec covers the underlying *class*.

The operator framing:

> I didn't realize there was a path for a bot to basically operate
> outside of the parameters and controls of the Evolve system. If
> that's the case this is a real gap.

## Principle

> When an app's `bot_guidance` directs the agent to invoke a
> bot-local script in response to a triggering message, the
> script — not the LLM — must be the response path. If the script
> can't run, the bot stays silent or posts the documented fallback;
> it does NOT substitute its own answer using general tools.

The bypass problem is **behavioral integrity**, not budget. Per-bot
`daily_cap_usd` + the [PR #1483](https://github.com/evolve-ops/evolve/pull/1483)
auto-trip already backstop cost. The controls that get bypassed
when the agent freelances — scope gates, source grounding, privacy
log, opt-out honor, per-sender rate limit — are about *what the
bot is and what it does*. None of them reduce to a per-app cap.

Per-app cost caps and per-app rate limits, where they exist today,
are abuse-mitigation knobs for specific external-facing surfaces
(atlas-research's $1/day cap exists because the bot replies in a
public Telegram community where strangers can spam @-mentions). They
are not user-facing budget primitives. See
``memory/feedback_per_app_vs_per_bot_cost_unit.md``.

## Affected apps

As of 2026-06-05:

| App | Trigger | Script | scheduled_actions backstop? | Class |
|---|---|---|---|---|
| `atlas-on-demand-research` | `@mention` / `/ask` / DM | `scripts/atlas_research.py` | no | **AT-RISK** |
| `atlas-article-capture` | URL in group message; `/optout*` | `scripts/atlas_capture.py` | no | **AT-RISK** |
| `atlas-daily-digest` | scheduled | `scripts/atlas_digest.py` | yes | hardened (cron-only) |
| `atlas-weekly-recap` | scheduled | `scripts/atlas_recap.py` | yes | hardened (cron-only) |
| gallery (`task-manager`, `ea-pack`, `unified-task-system`) | heartbeat / cron | — | yes | hardened |
| 8 migrated daemon apps via `migrate_v7.py` | scheduled | — | yes | hardened |

So the AT-RISK surface today is two apps, both atlas, both
already patched at the per-app level by PR #2192. The gap is in
the *pattern* — `bot_guidance` + agent-invoked script is the
natural shape for any future "respond to a chat trigger by
running a constrained script" app.

## Bypass surface

What gets bypassed when the agent freelances on an at-risk app:

| Layer | What an app's script enforces | What runs when bypassed |
|---|---|---|
| **Scope / topic gate** | scope-check + strategy-check refusals | LLM answers anything in scope of its general toolset. Bot drifts from "research surface" to "generic chatbot." |
| **Source grounding** | synthesis prompt forbids inventing sources; cites only verified URLs | LLM speaks from training data; no per-claim source. |
| **Privacy / audit log** | hashed member_id + outcome → app's log | App's log gains no entry. Operator review surface goes dark. |
| **Dedup / opt-out honor** | optout.json honored; archive consulted | Opt-out can be silently violated (re-archives URLs the member opted out of). |
| **Per-sender rate limit** | per-app `N/hour, M/day` per member | Unbounded — a single member can drown out the rest of the community. |
| **Audience guard** | DM strangers / foreign-group messages silently dropped | Agent's own audience-scoping may or may not catch; depends on AGENTS.md prose alone. |

Cost is intentionally absent from this list. Per-bot
`daily_cap_usd` is the right backstop for cost; per-app cost
attribution is not pursued as a control in this spec.

## Enforcement options considered

| | Mechanism | Status |
|---|---|---|
| A | Per-message tool restriction in OC | **Blocked upstream.** OC's `tools.profile` has 4 fixed values + bot- and per-agent-level allow/deny; no per-message or per-trigger hook. |
| B | Subagent with narrow tool policy | **Reachable.** OC has `subagents.tools.allow/deny` ([oc-config-schema.txt:18626](schemas/oc-config-schema.txt)). The parent agent still has to *choose* to spawn the subagent, so this lowers but doesn't eliminate bypass probability. Phase 2 candidate. |
| C | Pre-agent message intercept (plugin) | **Blocked upstream.** OC's `TurnObserver` exposes `agent_start` / `agent_end` / `llm_output` as observers, not interceptors. No `before_message` hook today. |
| D | Detection daemon — `agent_bypass_audit` Signal producer | **Phase 1.** Pure Python. Walks `agent_end` events daily, emits per-(bot, app) Signal when trigger-pattern messages didn't invoke the declared script. |
| E | Install-time validator — `bot_guidance_freelance_validator` | **Phase 2.** Mirrors `scheduled_actions_validator.py`. Gate gallery preflight + forge build on `bot_guidance` shape. |
| F | POD_CONDUCT.md hard-stop rule | **Phase 1.** Generalize PR #2192's per-app rule pod-wide via the existing `session_surface.py` injection channel. Policy not enforcement, but defends in depth at zero code cost. |
| G | Per-app cost attribution + tighter cap | **Out of scope.** Per-app caps are not the user-facing budget unit. |
| H | Bot/channel-level rate-limit-per-sender primitive | **Future, separate work.** Atlas's in-script rate limit is sender-state belonging at the channel boundary, not buried in app code. See ``memory/project_rate_limit_per_sender_as_bot_primitive.md``. |

## Phasing

### Phase 1 — visibility + policy (this week)

Two deliverables, neither blocks anything:

1. **`agent_bypass_audit` daily Signal producer.** Pure Python,
   modeled on
   [`reconcile_audit.py`](../packages/analyzer/reconcile_audit.py)
   and
   [`digest_source_audit.py`](../packages/analyzer/digest_source_audit.py).
   Runs daily as the `evolve` user.
   - **Inputs.** (a) Pod's installed-app registry at
     `{shared_dir}/applications/{bot_id}/`, filtered to manifests
     with `bot_guidance` declaring a script invocation. Read the
     `scripts/<name>.py` reference out of the bot_guidance prose
     so the audit knows what tool-call to look for. (b) Pod-wide
     `agent_end` records over the last 24h (already captured by
     `TurnObserver` and consumed by
     [`cost_event_converter.py`](../packages/analyzer/cost_event_converter.py)).
   - **Heuristic.** For each (bot, app) pair where the manifest is
     at-risk: identify recent messages whose text matches the
     app's trigger pattern (e.g., `@<bot_handle>` for
     atlas-research; a URL in a group message for atlas-capture).
     For each such message's turn, inspect the tool-call sequence.
     If the declared script path does NOT appear as an `exec`
     tool argument anywhere in the turn → that turn is a
     **bypass candidate**.
   - **Output.** One Signal per (bot, app) when bypass candidates
     are present in the window. Producer `agent_bypass_audit`,
     type `agent_bypass`, signature
     `agent_bypass:{bot}:{app}`, severity `warn`. Body cites the
     count of bypass candidates and one example message id.
   - **Resolution.** `signals.store.sweep_resolve` clears the
     Signal once the bot returns to compliance for the full
     window.
   - **Schedule.** Daily at 04:35 UTC, after `reconcile_audit`
     (04:30) and `proposal_auto_resolve` (03:45). Ordering matters
     to avoid bunching.

2. **POD_CONDUCT.md hard-stop rule.** Promote PR #2192's per-app
   instruction to a numbered pod-wide rule, injected into every
   session by `session_surface.py`. Wording target:

   > **App-routed responses.** If `AGENTS.md` (or any app's
   > guidance) tells you to invoke a specific script in response
   > to a triggering message, and the script invocation fails for
   > any reason, post the documented fallback (or stay silent if
   > the app's failure mode is silence) and STOP. Do not
   > substitute your own answer using general tools — the
   > script's controls exist for a reason.

   Added to both the marker block (injected text) and the full
   section below.

### Phase 2 — see sketch (supersedes the §below)

**See [`spec-agent-freelance-bypass-phase2-sketch-2026-06-05.md`](spec-agent-freelance-bypass-phase2-sketch-2026-06-05.md).**
The sketch supersedes the paragraphs below pending fold-back. Key
shifts:

- `before_prompt_build` is reachable from Evolve plugin code today
  (`TurnObserver.ts` already uses it for evo's stay-quiet /
  direct-send pattern), so **structural enforcement moves from Phase
  3 to Phase 2C** — the LLM never sees the trigger message in a
  state where freelance is possible.
- The subagent-narrowing path (originally Phase 2's third bullet) is
  **deferred** — both current at-risk apps emit finished replies and
  don't need LLM weaving on top of script output. No current use
  case for it.
- New structured `triggers[]` manifest field with per-entry
  `enforced: bool` replaces the originally-planned `invocation_mode`
  enum.

The paragraphs below remain for archaeology — don't act on them.

~~1. **`bot_guidance_freelance_validator`** modeled on
   [`scheduled_actions_validator.py`](../packages/admin/evolve_admin/applications/scheduled_actions_validator.py).
   Gate gallery preflight + forge `run_forge_job` Step 1 / apply
   reconciliation on manifests where `bot_guidance` declares a
   script invocation. The manifest must ALSO declare one of:~~
   - ~~A `subagents.tools.allow/deny` policy that narrows the
     subagent reachable from this `bot_guidance` section.~~
   - ~~A `scheduled_actions[]` entry covering the same script (the
     cron path is the natural backstop).~~
   - ~~An explicit `bot_guidance_freelance_tolerant: true` opt-out
     in the manifest, documenting that the operator accepts the
     bypass risk.~~
   ~~Refusal carries a `build_blocker` severity, as
   `scheduled_actions_validator` already does.~~
~~2. **Backfill sweep.** Run the validator across the installed-app
   registry; file `bot_guidance_validator` Signals (NOT blockers)
   for existing offenders so the operator can migrate at leisure.~~
~~3. **Opt-in `bot_guidance.invocation_mode: "subagent"`** manifest
   field. When present, the forge / install pipeline materializes
   the subagent tool policy in the bot's `openclaw.json` and the
   `bot_guidance` prose tells the agent to spawn the subagent.
   atlas-on-demand-research and atlas-article-capture become the
   reference adopters.~~

### Phase 3 — upstream-blocked closure

True closure requires either per-message tool policy or a
pre-LLM `before_message` plugin hook in OC. Neither exists today.
File the upstream feature request when Phase 2 is in.

## Non-goals

- Per-app cost attribution tightening. The per-bot
  `daily_cap_usd` is the budget primitive.
- Bot/channel-level rate-limit-per-sender. Tracked separately at
  ``memory/project_rate_limit_per_sender_as_bot_primitive.md``;
  natural follow-on once Phase 2's invocation_mode work is in.
- Retroactive remediation of historic freelance answers. The
  bypass log is recoverable from `agent_end` archives, but no
  operator-facing surface is in scope here.

## Open questions

- **Subagent fidelity.** Has anyone on this pod tested OC
  subagents end-to-end? Phase 2's invocation_mode is a no-op if
  the mechanism is rough. Worth a one-off canary on a low-traffic
  bot before recommending widely.
- **Trigger-pattern matching.** The audit needs a per-app trigger
  recognizer. Atlas-research is `^/?@<bot_handle>\b` or `^/ask\b`
  in a group; capture is "URL present in group message." Encoding
  this in the manifest (e.g., a structured `triggers[]` block)
  vs. inferring from prose is a Phase 2 question.
- **`bot_guidance` as a pattern.** Should agent-invoked scripts
  be deprecated in favor of pre-intercept (signal-subscriber /
  plugin) once Phase 3 lands? Convenient for the LLM but
  inherently policy-not-enforcement. Defer until Phase 1 data is
  in.

## Adjacent specs + memory

- [PR #2192 commit](https://github.com/evolve-ops/evolve/pull/2192) — JSON-request-file workaround that motivated this spec.
- [`docs/system/POD_CONDUCT.md`](system/POD_CONDUCT.md) — pod-wide conduct injection target for Phase 1's hard-stop rule.
- [`docs/system/RUNTIME_NOTES.md`](system/RUNTIME_NOTES.md) — tactical platform facts; out of scope for this spec (behavioral rule goes in conduct, not runtime).
- ``memory/feedback_per_app_vs_per_bot_cost_unit.md`` — why per-app cost is not pursued.
- ``memory/project_rate_limit_per_sender_as_bot_primitive.md`` — adjacent direction for rate-limit-per-sender at bot/channel level.
- ``memory/project_oc_exec_preflight_runtime_notes.md`` — upstream preflight context.
- ``memory/project_evo_oc_native_architecture.md`` — subagent + OC-native scaffolding context.
