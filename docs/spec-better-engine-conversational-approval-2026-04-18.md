# Better Engine — Conversational Approval (2026-04-18)

> **Status update — 2026-05-07 (slice 5b8 day 2 shipped):** day-1 delivered
> the foundation; day-2 closed the punch list (config knobs, TTL, snooze
> duration, voice cues, push scaffolding, plugin cleanup). The original
> spec below is preserved verbatim for context; the **Implementation
> status** section at the top supersedes §3, §4, §6, and §8 in places —
> that section is the source of truth for what's currently in code.

## Implementation status (2026-05-07)

The conversational-approval mechanism does not ship as a parallel parser
alongside the wizard — it ships **as a wizard phase**, reusing the
extractor / state / classifier / audit / prompt machinery built by slices
5a–5b7.

**Day 1 (shipped):**

- `evolve_admin/evo/wizard/intent.py` — two-stage intent pipeline. Stage 1
  is a deterministic PHRASES + WORDS classifier with the same split
  pattern `engine.py` uses for GUIDE_CONFIRM / GALLERY_RECS (so "no" inside
  "not sure" doesn't false-fire). Stage 2 calls Anthropic Haiku via the
  extractor's `_call_anthropic` helper, returning JSON-shaped
  `{action, confidence, snooze_hint_days, rationale}`. Test seam:
  `set_intent_parser` mirrors `set_extractor`. Confidence threshold 0.80;
  stage 2 gated by length / no code blocks / no URLs.
- `evolve_admin/evo/wizard/phases.py` — new `PHASE_REC_PENDING` phase
  (terminal, deterministic, same shape as PHASE_GUIDE_CONFIRM and
  PHASE_GALLERY_RECS).
- `evolve_admin/evo/wizard/state.py` — new `Audience` value `"approver"`
  for rec_pending sessions; finalize does **not** commit a user profile
  (mirrors `guide_drafter`).
- `evolve_admin/evo/wizard/engine.py` —
  - `start_rec_pending(shared_dir, *, bot_id, user_key, rec, surface)`
    initializes the session; the rec is stashed under
    `state.extracted["_pending_rec"]` (underscore-prefixed scratch).
    Empty queue → finalize same turn with the `all_caught_up` variant.
  - `_handle_rec_pending(...)` classifies the reply via `intent.parse_intent`,
    routes accept/reject/snooze/next through `BetterEngine.record_feedback`
    /  `engine.snooze` (server-side; no plugin HTTP roundtrip), chains to
    the next rec on the same surface, and finalizes when the queue empties.
    Audit events written for each action via `evo.audit.append_event`
    (`rec_accept`, `rec_reject`, `rec_snooze`, `rec_next`).
  - `_finalize_rec_pending(...)` mirrors `_finalize_guide`: marks state
    completed, strips the underscore-prefixed scratch keys, no profile
    commit.
  - Bot guide loaded into prompt context for `PHASE_REC_PENDING` so the
    pitch can pick up the bot's declared tone (this is the §5
    voice-source plumbing, partial: SOUL.md / user profile come day 2).
- `evolve_admin/evo/wizard/prompts.py` — `_rec_pending_block` with four
  variants: `pitch` (first turn), `clarify` (last reply ambiguous;
  re-ask with parser's best guess), `context` (user asked for more
  info), `all_caught_up` (terminal, queue empty). Public helper
  `build_rec_pending_block(...)` for callers outside the phase switch.
- `evolve_admin/evo/dispatch.py` — bare `evo` and `evo better` now route
  through `_start_rec_pending_dispatch`, which fetches the top
  recommendation server-side (`BetterEngine.get_top(surface="member_bot",
  scope_id=bot_id)`), starts the wizard session, and returns
  `mode="speak"` with `wizard_session_id`. **The `mode="legacy_better"`
  return is retired for the happy path.** It survives only as a safety-net
  fallback when the BetterEngine import / construction fails mid-deploy
  (the plugin's `_runLegacyEvoBare` honors it).
- `packages/plugin/src/observer/TurnObserver.ts` — the `parsedEvo.isBare ||
  parsedEvo.subcommand === "better"` short-circuit at line ~1288 is
  removed; every evo command goes through `evoDispatchClient.dispatch`.
  When the dispatcher returns `mode="legacy_better"` the plugin runs
  `_runLegacyEvoBare`, a thin helper that mirrors the original
  fetchAndFormatTopRec flow. Wizard-session capture (`state.wizardSessionId`)
  works unchanged — every subsequent user turn routes through
  `/api/evo/wizard/turn` and into `_handle_rec_pending`.
- `packages/admin/tests/test_evo_wizard_rec_pending.py` — 54 tests:
  stage-1 keyword hits across all five actions, ambiguity preempt
  ("not sure", "no idea"), single-letter shortcuts gated on
  pending-rec-exists, confidence clamping, snooze duration extraction,
  start_rec_pending populated + empty-queue paths, accept/reject/snooze/
  next routing through the BetterEngine, context re-renders without
  state transition, unknown clarifies once then finalizes on the second
  miss, dispatcher routes both bare evo and `evo better` plus the
  legacy_better fallback when the engine is unreachable.

**Day 2 (shipped 2026-05-07):**

- **Plugin: gutted Case 2 follow-up branch.** [TurnObserver.ts:~1397](packages/plugin/src/observer/TurnObserver.ts) —
  the `state.pendingRecId && state.pendingRec` branch that called
  `betterFormatter.parseReply` for legacy follow-ups is replaced with a
  defensive cleanup that clears any stale plugin-side rec state. Wizard
  sessions now own all follow-ups; legacy state from pre-deploy
  sessions fades after the next user turn.
- **Config knobs.** Added `conversational_approval` block to
  [`better_engine_config._COMPILED_DEFAULTS`](packages/analyzer/better_engine_config.py)
  with the spec's six keys (`enabled`,
  `llm_intent_parse_enabled`, `confidence_threshold`,
  `default_snooze_days`, `pending_expiry_minutes`, `push_preamble_enabled`).
  New module
  [`evolve_admin/evo/wizard/config.py`](packages/admin/evolve_admin/evo/wizard/config.py)
  exposes a `ConversationalApprovalConfig` dataclass and `resolve(shared_dir, bot_id)`
  helper with safe-default fallbacks when analyzer isn't on sys.path or
  the config file is malformed. Engine reads the resolved cfg in
  `_handle_rec_pending` and threads it through to `intent.parse_intent`
  (LLM toggle, confidence threshold) and `_record_rec_action`. Dispatcher
  `_start_rec_pending_dispatch` honors `enabled=False` by returning
  `mode="legacy_better"` — operator escape hatch.
- **Pending-rec session TTL.** `process_turn` checks `state.audience ==
  "approver"` and compares `state.updated_at` against
  `pending_expiry_minutes` (default 60 — a conversational window, not a
  days-scale one; the original 7-day default caused stale pitches to
  hijack the user's next unrelated message into a clarify loop). Stale
  sessions get marked completed and return `None` (plugin clears its
  session reference; bot resumes normal flow). Only approver-audience —
  primary / guide_drafter wizards remain long-lived. The pending rec
  stays in the better-engine queue for re-entry via `evo`. The
  `/api/evo/wizard/active` recovery probe applies the same expiry
  check so post-restart plugins don't re-attach to stale sessions
  either.
- **Snooze duration end-to-end.** [`better_engine/snooze.py`](packages/admin/evolve_admin/better_engine/snooze.py)
  `snooze_recommendation(rec, *, days_override=None)` now accepts an
  override; [`better_engine/engine.py`](packages/admin/evolve_admin/better_engine/engine.py)
  `BetterEngine.snooze(rec_id, *, days_override=None)` forwards it. The
  wizard handler passes `result.snooze_hint_days` (set when stage 2
  parses a duration like "two weeks" → 14) without override falling
  through to the engine's escalation schedule, so the schedule (1, 2,
  4, 7) still kicks in for naked "snooze" / "later" replies.
- **Voice tone wiring.** New module
  [`evolve_admin/evo/wizard/voice.py`](packages/admin/evolve_admin/evo/wizard/voice.py)
  with `voice_summary(shared_dir, bot_id, *, bot_guide=None)` reading
  from three sources: bot guide frontmatter (already loaded into prompt
  context), `<bot_home>/.openclaw/workspace/SOUL.md` (extracts the `##
  Tone` section), and `{shared_dir}/profiles/<bot_id>.md` (extracts the
  `## Communication Preferences` section, skipping `(empty)`
  placeholders). Engine `_build_prompt` invokes `voice_summary` for
  rec_pending phases and stuffs the result into prompt context as
  `voice`. `_rec_pending_pitch_block` formats the cues into a
  one-bulleted "Voice cues from this bot's setup:" block in the
  systemAppend.
- **Push preamble scaffolding.**
  [`engine.start_push_preamble(...)`](packages/admin/evolve_admin/evo/wizard/engine.py)
  takes a `(shared_dir, bot_id, user_key, network)` and starts a
  rec_pending session with the new `push` prompt variant when (a)
  config gates it on, (b) there's a queued urgent rec
  (urgency:critical/high or type:security_critical/operational_urgent
  per `_is_urgent_rec`), and (c) no wizard session is already active.
  Returns `None` and writes no state otherwise. The plugin doesn't yet
  call this — wiring outbound push delivery is its own spec — but the
  server-side mechanism is in place and tested.

**Day 2 deferred (out of scope for slice 5b8):**

- Plugin call site for `start_push_preamble`. Real push delivery wants
  per-channel rate-limiting and an explicit per-bot frequency cap;
  treat that as a separate spec. The scaffolding above is dormant until
  it ships.
- Stage-2 LLM-call observability (cost / latency tracking). Today the
  Haiku call is fire-and-fall-back-to-unknown; richer instrumentation
  belongs with the existing Anthropic-call telemetry surface.

**Architectural deltas from the original spec:**

- §3.2 had stage 2 calling a NEW `/api/better-engine/parse-intent` endpoint.
  The shipped design folds it into the wizard turn loop instead; the
  `intent.parse_intent` function runs server-side inside the wizard's
  existing `/api/evo/wizard/turn` route. No new endpoint.
- §3.4 plugin TS delegation is gone — the plugin doesn't parse intents at
  all anymore. It hands the user's raw text to the wizard turn endpoint
  and the server-side handler does everything.
- §4.1 "next maps to dismissed" — implemented as `record_feedback(rec_id,
  "rejected", reason="ignored")` so the existing learning layer keeps the
  soft-dismiss / hard-reject distinction it already had.
- §6.3 unrelated-reply handling — implemented via the
  `_unknown_streak` counter on `state.extracted`. First miss re-renders
  with `clarify`; the second consecutive miss finalizes without action,
  letting the bot resume normal conversation. The rec stays pending in
  the queue.

---

Status: implementation spec. Ships after L3 (needed to make `bot_primary_user`-audience proposals actionable by Type 2 users who have no admin UI).

**Parent:** [Better Engine — Architecture (2026-04-17)](spec-rsi-architecture-2026-04-17.md).
**Related:**
- [spec-better-engine-arbiter-bridge-2026-04-18.md](spec-better-engine-arbiter-bridge-2026-04-18.md) — the admin-UI equivalent; conversational approval is the mirror for Type 2 users.
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md) — the wizard engine that this slice extends with `PHASE_REC_PENDING` (added 2026-05-06).
- [project_user_types_and_approval](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_user_types_and_approval.md) — the three user types and why conversational approval is essential for Type 2.

## 1. What exists already

The `evo`/`evolve` keyword infrastructure is working. Pod-admin invested significant effort making it reliable. Specifically:

- [KeywordHandler.ts:31](packages/plugin/src/better/KeywordHandler.ts:31) detects the keyword (exact match, lower-case, trimmed).
- [KeywordHandler.ts:84](packages/plugin/src/better/KeywordHandler.ts:84) `handleFollowUp` already handles five actions: `accept`, `reject`, `snooze`, `next`, `context`.
- [RecommendationFormatter.ts:81](packages/plugin/src/better/RecommendationFormatter.ts:81) `parseReply` maps user messages to actions via **hardcoded keyword lists**:
  - Accept: `"yes" | "accept" | "do it" | "ok" | "sure"`
  - Reject: `"no" | "reject" | "skip" | "nope" | "pass"`
  - Snooze: `"snooze" | "later" | "not now" | "remind me"`
  - Next: `"next" | "more" | "➡️"`
  - Context: `"why" | "context" | "more info"`
  - Plus single-letter shortcuts (`a`, `s`, `n`) when a recommendation is pending.
- [TurnObserver.ts:1061](packages/plugin/src/observer/TurnObserver.ts:1061) manages session state (`pendingRecId`, `pendingRec`) across turns.

This works great when users happen to use exact matches. It fails when they write naturally: "yeah go for it", "not this week, come back later", "I'm not sure that's right", "tell me what you mean by that." The existing parser returns `null` on those and the interaction stalls.

## 2. Scope

### In scope
- **LLM-assisted intent parser**: two-stage parser (hardcoded first, cheap LLM fallback when hardcoded returns null) that maps natural-language user replies to the existing `FollowUpAction` enum.
- **Multi-intent handling**: a reply like "yes, but tell me more first" is ambiguous; the parser prefers clarification over action.
- **Confidence gating**: below a threshold, the bot asks for clarification rather than acting.
- **Response-voice customization** via the user profile: replies should match the bot's voice as declared in SOUL.md and the user's communication_preferences (direct/warm/etc.).
- **State-transition bridge**: the `FollowUpAction` enum maps to the four arbiter state transitions (Act / Dismiss / Reject / Snooze). Clarifying exactly which is which.
- **Push mode scaffolding** for later: when an urgent proposal arrives, the bot proactively pitches it in the next user interaction. L3+L6 have proposals; push delivery is future work but the spec notes the design hook.

### Out of scope
- **Rich conversational flows** beyond simple action detection. The bot doesn't enter a Q&A about the proposal; it pitches, parses, acts.
- **Full push mode**: proactively sending a message to the user without their initiating an interaction. Scaffolded here; implementation is a follow-up.
- **Per-user learning of intent phrasings**: if a specific user always says "bet" to mean accept, we don't learn that. Static intent model.
- **Voice** (audio) interfaces. Text only.
- **Multi-proposal conversations**: user doesn't stack decisions across multiple proposals in one message. Each proposal is a discrete exchange.

### Non-goals
- Replacing the `evo`/`evolve` pull model. This spec preserves pull as the primary interaction; it only enriches the parsing.

## 3. The intent parser

### 3.1 Two-stage design

Single function `parseReply(text, pendingRecExists)` that replaces the current pure-hardcoded implementation with a two-stage pipeline:

1. **Stage 1 — Hardcoded fast path** (unchanged from today). If an exact/substring match against the existing keyword lists returns an action, use it. Zero cost, zero latency.
2. **Stage 2 — LLM intent parse** (new). If stage 1 returns `null` and `pendingRecExists` is true and the message looks like a reply (< 200 chars, no keyword triggers outside our vocabulary), call a Haiku intent parser.

Stage 2 is gated by cheap heuristics:

- Length (short messages are more likely to be replies)
- Absence of unrelated content markers (URLs, code blocks, long quotes → probably not a reply to our pitch)
- Optional: a similarity-to-recent-pitch check

If stage 2 returns a low-confidence result, the bot responds with a clarification question ("I heard... — did you want me to go ahead, skip it, or come back to it later?") rather than picking blindly.

### 3.2 The LLM intent parse

Prompt shape:

```
You are parsing a user's reply to a specific suggestion from their assistant.

SUGGESTION SHOWN TO USER: {pending_rec.conversational_pitch}

USER REPLY: {user_message}

Return JSON: {"action": "accept" | "reject" | "snooze" | "next" | "context" | "unknown", "confidence": 0..1, "rationale": "one sentence"}

- "accept": user wants the suggestion applied
- "reject": user definitively doesn't want the suggestion
- "snooze": user wants to defer; not rejecting outright
- "next": user wants to see a different suggestion
- "context": user wants more information before deciding
- "unknown": the reply is ambiguous or unrelated
```

Haiku call, temperature 0.1, max_tokens 100. Cost per call: ~$0.0005. Even at one call per pending-rec interaction, bot-wide cost is trivial.

### 3.3 Confidence gating

```python
parseResult = {"action": ..., "confidence": 0..1, "rationale": str}

if stage1 hit: act on stage1 result
elif stage2.action != "unknown" and stage2.confidence >= 0.8: act on stage2 result
else: ask for clarification
```

Clarification response uses a template that includes the parser's best guess:

> "Just checking — did you want me to {best_guess_verb}, come back to it later, or skip it for now?"

This lets the user correct with a one-word response that stage 1 can handle.

### 3.4 Module layout

Parser expands from pure TypeScript to a TS entry point that optionally delegates to a Python helper for the LLM call. Actually — the existing plugin infrastructure already calls out to the admin backend for `acceptRecommendation` / `rejectRecommendation` etc., so we can delegate stage 2 to a backend endpoint:

```
POST /api/better-engine/parse-intent
{
  "user_message": "yeah let's try it",
  "pending_pitch": "I could track fiber..."
}
→ {"action": "accept", "confidence": 0.92, "rationale": "..."}
```

This keeps LLM keys and call logic server-side (plugin doesn't need to know about Anthropic API). Latency budget: 500ms typical, 2s p95. If the endpoint times out, fall back to stage 1 result (null) and ask for clarification.

## 4. State transition mapping

The existing `FollowUpAction` enum predates the full L1 state machine. Mapping each to the arbiter transitions:

| FollowUpAction | Arbiter state transition | Note |
|---|---|---|
| `accept` | `pending` → `approved_human` → apply pipeline | Maps to the "Act" user action |
| `reject` | `pending` → `rejected` | Hard negative; tracked for calibration |
| `snooze` | `pending` → `snoozed` with `snoozed_until = +N days` | Default snooze period 3 days (was "couple of days" in existing code); config-adjustable |
| `next` | Current rec → soft-dismiss (`dismissed`) + surface next in queue | Maps to "Dismiss" — soft negative |
| `context` | No state transition | Show extended context, stay on same rec |

### 4.1 The dismiss/next ambiguity

In the existing code, `next` means "show me something else" — the current rec gets `recordIgnored` (a soft-dismiss signal). In the L1 state machine, `dismissed` is a distinct terminal state. So:

- `next` action → transition current proposal to `dismissed`, fetch next pending.
- This is exactly the Dismiss user action from L1 §3.6 / Better Engine §9.

The mapping is clean; just making sure the existing `recordIgnored` call is wired to transition state machine correctly.

### 4.2 Snooze duration

Existing code snoozes for "a couple of days" (hardcoded). L1 `Proposal.snoozed_until` wants an explicit timestamp. Default mapping:

- Parsed intent "snooze" → `snoozed_until = now + 3 days`
- If user specified a duration naturally ("remind me next week" / "ask me tomorrow"): the LLM parser returns a hint in the rationale field; the backend sets `snoozed_until` accordingly.

Natural-duration extraction is part of the stage 2 LLM parse:

```json
{"action": "snooze", "confidence": 0.9, "rationale": "user asked for next week", "snooze_hint_days": 7}
```

If `snooze_hint_days` is present, use it; otherwise default to 3.

## 5. Response voice

The bot's response to user actions should sound like the bot (not like "System accepted request."). The response templates today in `handleFollowUp` are adequate but generic. This spec tightens:

### 5.1 Voice sources

When generating the response template filling, consult:

- SOUL.md of the bot — voice and personality declarations.
- User profile `communication_preferences` section — user's stated preferences (direct, warm, minimal emoji, etc.).
- Audience — responses to single-user bot owners differ from responses to multi-user team channels (the latter are more neutral).

### 5.2 Template shapes

Acceptance:

- Personal bot: "Got it — on it now." (or warmer/shorter based on voice)
- Team bot: "Queued — I'll handle it." (neutral)

Rejection:

- Personal bot: "No problem, skipping that."
- Team bot: "Noted — skipping."

Snooze:

- Personal bot: "Sure, I'll come back to it in {duration}."
- Team bot: "Snoozed for {duration}."

Clarification (ambiguous):

- Personal bot: "Hmm — did you mean go ahead, skip, or come back later?"
- Team bot: "Please clarify: accept, skip, or snooze?"

### 5.3 Template filling

Templates live in the plugin's `pitches.ts` (new) or extend `KeywordHandler.ts`'s existing inline strings. No LLM needed for filling — these are small slot-fills.

## 6. Session state and edge cases

### 6.1 Long pending windows

If a proposal is pitched but the user doesn't reply for N days (default 7), the bot's session state `pendingRec` should expire. The user is unlikely to remember what "yes" refers to a week later; better to re-pitch.

Implementation: TurnObserver checks `pendingRecSurfacedAt` timestamp; if > 7 days, treats as expired and the next `evo` invocation surfaces fresh.

### 6.2 Multi-intent replies

"Yes but first tell me more about what it would do" — contains both "accept" and "context" signals. Stage 2 LLM parser returns the higher-confidence primary intent; if equally split, returns `unknown` and the bot asks for clarification.

### 6.3 Unrelated replies

User types `evo`, sees a proposal, and then their next message is about something totally unrelated ("what's the weather"). The parser returns `unknown` low-confidence; the bot responds naturally to the unrelated message and the pending rec stays pending until next `evo` or explicit action.

### 6.4 Silent accept / silent reject

Some users may reply in ways that clearly accept without using any of our keywords — "let's do it!" — stage 1 misses (not in list); stage 2 catches. Others may just ignore and go on with their lives. In both cases, the pending rec stays pending until the user addresses it, `evo`'s again, or the 7-day expiry kicks in.

## 7. Push mode (scaffolding only — not full implementation)

The architecture described a push mode: "the bot can also *push* proactively when something urgent surfaces (Hey, I noticed X — want me to Y?)." This spec scaffolds the hook but doesn't ship the full feature.

### 7.1 Scaffolding

Add to TurnObserver a check: at the start of a user-initiated message, if there's a pending `security_critical` or `operational_urgent` proposal for this bot with audience `bot_primary_user` and no current `pendingRec` in session, inject a preamble into the bot's next response:

> "Before we dive in — I noticed {conversational_pitch}. Want me to handle that, or later?"

The user's next message gets parsed by the same intent parser as pull-mode.

### 7.2 What's not here

- Push notifications (actually sending a message to the user when they haven't initiated). That's a bigger deal — needs channel-specific delivery (Telegram, Slack, etc.), careful rate limiting to avoid annoying users, probably a separate `push_frequency_cap` config per bot. Deferred to its own spec.
- Cross-session pushing (tapping the user on the shoulder between sessions). Same concern.

The scaffolding above just rides existing user-initiated messages, which is safe.

## 8. Configuration

New section in `better-engine-config.json`:

```jsonc
{
  "pod_defaults": {
    "conversational_approval": {
      "enabled": true,
      "llm_intent_parse_enabled": true,
      "confidence_threshold": 0.80,
      "default_snooze_days": 3,
      "pending_expiry_minutes": 60,
      "push_preamble_enabled": false  // scaffolded, not live
    }
  },
  "bots": {
    "team-bot-a": {
      "conversational_approval": {
        "default_snooze_days": 1  // team-bot-a user prefers shorter snooze
      }
    }
  }
}
```

Per-bot overrides allowed. LLM intent parse can be disabled globally to save cost (falls back to hardcoded-only).

## 9. Testing

### 9.1 Unit tests (backend intent endpoint)

- Parse accept: ≥ 10 varied phrasings ("yes", "yeah", "ok let's do it", "go for it", "sure", "perfect", "sounds good") all return `accept`.
- Parse reject: ≥ 10 varied phrasings.
- Parse snooze: variations including duration hints ("not today", "next week", "ask tomorrow" → correct snooze_hint_days).
- Parse context: "why", "tell me more", "what does that mean".
- Parse unknown: unrelated messages ("what's the weather", long text about an unrelated topic).
- Confidence gating: mock low-confidence responses → expect clarification path in caller.
- Multi-intent: "yes but tell me more" → returns clarification or highest-confidence single intent.

### 9.2 Plugin-side tests

- `parseReply` stage 1 fast path: each hardcoded keyword list exhaustively tested.
- `parseReply` stage 2 integration: backend endpoint mocked; test delegation logic, timeout fallback, null handling.
- Session state: pending rec expiry at 7 days.
- Multi-turn: pitch → clarification → final action.

### 9.3 Integration tests

- End-to-end accept via natural language: user says "evo" → bot pitches → user says "yeah let's try it" → backend intent parse returns accept 0.9 → proposal transitions to approved_human → apply pipeline runs.
- End-to-end reject: similar with "nah not for me" → `rejected`.
- End-to-end snooze with duration: "not this week" → `snoozed` with `snoozed_until = +7d`.
- End-to-end ambiguity: "hmm not sure" → clarification question emitted; user's follow-up "yes ok" → accept.
- End-to-end long-pending expiry: pitch → wait 8 simulated days → user types something → rec expired, no stale ghost pending.

### 9.4 Adversarial fixtures

Small set of tricky replies:
- "I don't know"
- "Maybe, what do you think?"
- "Can you just do what's best?" (deferred authority)
- "Only if it doesn't cost much" (conditional)
- "Go on" (could mean "accept" or "tell me more")
- Typos: "yse", "ok go ahaed"

Review parser behavior on each; tune prompt or keyword lists based on findings. These aren't pass/fail tests; they're a calibration artifact.

### 9.5 Manual walkthrough

1. Send `evo` to a member bot on the test bed; observe pitch.
2. Reply with exact keyword ("accept"); observe transition.
3. Reset pending. Reply with natural phrase ("sounds good"); observe LLM parse in backend logs; observe correct transition.
4. Reset. Reply ambiguously ("ok but tell me more"); observe clarification.
5. Reply "remind me next week"; observe snooze_until set to +7d.
6. Let a pending rec expire (fast-forward clock if needed); observe state clears.

## 10. Acceptance criteria

- [ ] Backend `/api/better-engine/parse-intent` endpoint implemented with Haiku call
- [ ] Plugin `parseReply` extended with stage 2 delegation + timeout fallback
- [ ] Natural-language duration hints (`snooze_hint_days`) parsed and honored
- [ ] Session state expiry at 7 days (configurable) functional
- [ ] Clarification path when confidence below threshold
- [ ] Response voice templates consult SOUL + user profile
- [ ] Config knobs in `better-engine-config.json` respected
- [ ] Push preamble scaffolded (behind `push_preamble_enabled=false` default)
- [ ] Unit tests green, coverage ≥ 90% on new modules
- [ ] Integration tests green including end-to-end flows
- [ ] Manual walkthrough (§9.5) completed cleanly
- [ ] Documentation updated: both spec and any user-facing explanation of how `evo` works

## 11. Risks and open questions

### Risks

- **Stage 2 cost creep.** If every pending interaction triggers a Haiku call, costs stay cheap but monitor. Mitigation: stage 1 catches most; stage 2 gated by heuristics.
- **Misinterpretation damages trust.** Bot accepts when user meant "tell me more" → applies an unwanted change → user loses confidence. Mitigation: confidence threshold at 0.80; clarification on ambiguity; auto-revert handles the damage for most changes.
- **Latency on stage 2.** 500ms typical, 2s worst — noticeable in conversational flow. Mitigation: timeout fallback to clarification; optimize prompt for brevity.
- **Response voice inconsistency.** Templates may not match a bot's SOUL perfectly. Mitigation: Persona Tuner (L6) may surface proposals to adjust the templates if patterns of user correction emerge.

### Open questions

- **Should stage 2 be available when `rsi.enabled = false`?** The conversational approval mechanism is useful even for a minimal pod (it handles sysadmin proposals routed to bot_primary_user). Recommendation: stage 2 follows `better_engine.enabled` (the outer toggle), not `rsi.enabled`. Guardians produce proposals regardless of RSI; the approval interface must work.
- **Can the user customize keyword mappings?** ("In my pod, 'lgtm' always means accept.") Recommendation: not in this spec; user profile could eventually carry per-user synonyms but defer.
- **What happens if the LLM parse disagrees with a hardcoded match?** Currently stage 1 always wins. Recommendation: keep that invariant — stage 1 is deterministic and fast; overriding via LLM would introduce inconsistency.
- **Push mode scope escalation.** The scaffolded preamble might tempt expansion. Recommendation: hard hold the line — push notifications (actual outbound messages) are their own spec with careful rate limiting.
- **Team bot context** (`role="member", multi_user=True`). Proposals with `approval_audience=bot_primary_user` in a team bot — whose reply counts? Probably the primary user's only; other team members' replies shouldn't accept on their behalf. Need to check current `evo` behavior on team bots; if it's working, preserve; if not, this spec doesn't fix it (separate work).

## 12. Future

- **Full push mode** — proactive outreach when critical proposals land. Separate spec, careful rate-limiting, per-channel delivery.
- **Per-user intent learning** — if a user consistently uses idiosyncratic phrasings, learn them. Requires aggregation across many proposals; natural fit with Persona Tuner in L6+.
- **Rich conversational flows** — back-and-forth where the bot can answer follow-up questions before the user decides. More complex; would probably involve running a structured dialogue with LLM-generated responses. Significantly larger scope.
- **Approval by delegation** — "let the admin decide" as a user action, routing the proposal to pod_operator. Useful for multi-user bots where the user isn't sure they should approve. Small addition once the foundation ships.
