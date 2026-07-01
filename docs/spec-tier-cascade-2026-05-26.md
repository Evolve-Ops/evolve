# Tier Cascade — design spec

**Status:** Draft for discussion · 2026-05-26
**Author:** pod-admin + claude (design dialogue)
**Replaces:** the productive/maintenance session-classification pipeline at `packages/plugin/src/observer/TierClassifier.ts` and `LLMTierClassifier.ts` (deprecated by this spec)
**Forces adoption of:** Opik hot-path span emission (extends infrastructure already shipped at `packages/analyzer/observability/opik_client.py`)
**Sibling spec:** [docs/spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md) — covers *operator-driven* tier override via UI chip (Auto/Fast/Standard/Power). Already shipped as PR #1629. This cascade spec covers the *automatic* (Auto) behavior that runs when the operator hasn't explicitly chosen a tier. The two specs share the `ModelRouter.setUserTier()` primitive — cascade does not invent a new tier-override mechanism.
**Motivating memory:** [feedback_rsi_design_approach](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_design_approach.md), [feedback_rsi_low_cost_preference](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_low_cost_preference.md), [v1_1_substrate_adoption_priority](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_v1_1_substrate_adoption_priority.md)
**Adjacent memory:** [project_cost_alerting_blackout_2026_05_20](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/project_cost_alerting_blackout_2026_05_20.md) (the silent-misclassification family)

---

## 1. Problem + principles

### 1.1 What's broken

The current session-tier classifier ([packages/plugin/src/observer/TierClassifier.ts](../packages/plugin/src/observer/TierClassifier.ts)) decides at turn 1, from the first user message, whether a session is "productive" or "maintenance" — then maps that label to tier2 (Sonnet) or tier3 (Haiku) and never revisits. Three structural problems:

1. **The categorical abstraction is wrong.** Productive/maintenance is a proxy for "how hard is the work" but a leaky one. A debugging session against a flaky launchd race needs Sonnet or Opus. A "what's on my calendar today" session is trivially Haiku. Mapping intent-category to model-tier conflates orthogonal axes.

2. **Single-shot prediction can't adapt.** A session that opens "what's on my calendar" and pivots at turn 4 to "actually let's plan the whole quarter" stays on tier3 (Haiku) for the rest of the session because the verdict was locked at turn 1. The current code has no re-classification path.

3. **No feedback loop.** The system has no labeled corpus, no accuracy metric, no way to know it got the verdict right. Misclassifications are only surfaced when cost anomalies fire — and even then, the link from anomaly to classifier verdict is detective work, not telemetry. The `{sharedDir}/calibration/classifier.json` file is loaded but never written; the admin UI literally exposes `"calibration_writer_implemented": False`.

The 2026-05-20 cost-alerting blackout was not a classifier failure (it was OC heartbeat override loss + tier-3 audit orphan workers), but it sits in the same family: **silent misconfiguration that only becomes visible downstream**. The cascade design closes that whole class of failure mode.

### 1.2 The literature, briefly

This is a solved problem with a name: **LLM routing**. Two architectures dominate:

- **Predictive routing** (RouteLLM, NotDiamond) — classify upfront. Trained on labeled (query, which-model-won) pairs from Chatbot Arena. Reported wins: 95% of GPT-4 quality at 14% GPT-4 usage. Structurally what we do today, just with a much better classifier.
- **Cascading** (FrugalGPT, Cascadia) — don't classify upfront. Run the cheap model. Score the response. Escalate if it's not good enough. Reported wins: matching GPT-4 quality at **98% cost reduction**.

The cascading approach is a substantially better fit for Evolve's situation because:

- We're in a multi-turn agentic loop, not a single-query routing problem. Per-turn cascade is natural.
- Response quality is *observable post-hoc* via tool-error rates, retry patterns, plan drift — we don't need labels.
- Pre-trained classifier weights (e.g., RouteLLM's) were trained on Chatbot Arena single-turn conversational data and don't transfer cleanly to "should this OpenClaw session run on Haiku or Sonnet for the next turn."

Recent work also validates two of the mechanisms we'd build on:

- **"LLMs Encode Their Failures: Predicting Success from Pre-Generation Activations" (2026)** — model activations literally encode failure prediction. We don't have activation access on hosted APIs, but the behavioral proxy (retries, dead-end tool calls, token-spinning) is the externally-visible version.
- **"Knowledge Distillation in Automated Annotation" (2024)** — classifiers trained on LLM-generated labels perform within noise of human-labeled ones (96.1% agreement) for well-separated tasks. Supports a Haiku-as-judge audit loop.

### 1.3 Principles

1. **Cascade, don't predict.** Default cheap. Escalate on observed struggle. The categorical productive/maintenance label is the wrong abstraction and gets retired by this design.

2. **Cheap by default.** Struggle detection is pure Python over events we already emit. LLM-as-judge audit is async, sampled, low-frequency. Per memory [feedback_rsi_low_cost_preference](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_rsi_low_cost_preference.md) — cost-monitoring infra that itself burns budget defeats the point.

3. **Verify before shipping.** Three-phase rollout: telemetry only → shadow cascade → live cascade. Each phase produces measurement before the next ships. Per [feedback_two_pass_review_workflow](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_two_pass_review_workflow.md).

4. **The bot reasons about user intent; the controller reasons about machine signals.** The LLM is good at parsing "think harder about this please" out of a Telegram message. Pure Python is good at counting tool retries. Don't ask either to do the other's job.

5. **Plex test.** Configuration is layered with safe defaults. Operators with no opinion get the right thing. The UI never says "tier."

---

## 2. Architecture

Three components, each single-responsibility:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          OpenClaw session (per turn)                     │
│  ┌────────────┐    ┌──────────────────┐    ┌──────────────┐             │
│  │ user input │ ─► │ Cascade Ctrlr     │ ─► │ model call   │             │
│  └────────────┘    │ (before_resolve)  │    └──────┬───────┘             │
│                    └──────────────────┘           │                     │
│                            ▲                       ▼                     │
│                            │              ┌──────────────────┐          │
│                            │              │ Struggle Detector│          │
│                            │              │ (turn_end hook)  │          │
│                            │              └────────┬─────────┘          │
│                            │                       │                     │
│                            └───────── escalate ────┘                     │
│                                                    │                     │
│                                                    ▼                     │
│                                            ┌──────────────┐              │
│                                            │ Opik span    │              │
│                                            │ (hot path)   │              │
│                                            └──────┬───────┘              │
└───────────────────────────────────────────────────┼─────────────────────┘
                                                    │
                                                    ▼
                              ┌──────────────────────────────────────┐
                              │  Audit Layer (async, daily)          │
                              │  Haiku reads sampled sessions,        │
                              │  rates routing decisions, proposes    │
                              │  threshold tweaks. Output: calibration│
                              │  report + optional auto-tuning.       │
                              └──────────────────────────────────────┘
```

### 2.1 Component: Struggle Detector

**Lives in:** `packages/plugin/src/observer/StruggleDetector.ts` (new)
**Hooks:** OC's `agent_end` event — reads `event.messages` directly (Anthropic content blocks carry `tool_use` + `tool_result` inline). No file read required. See § 6.
**Output:** a `StruggleSignal` per turn, emitted to the cascade controller and to Opik

**Source-agnostic in Phase 1.** The detector itself does not branch on `trigger_kind` — it produces raw features + a score using default weights, and downstream consumers re-weight per source if needed (§ 2.4 + § 4.3 carry per-source weight overrides for Phase 2). This is deliberate: Phase 1 collects unbiased data so Phase 4's audit layer has signal-distribution evidence to tune weights against. Baking source-specific weights in now would presuppose tuning we haven't earned.

**Feature applicability differs by source (informational; weights only).** `clarification_loops` cannot fire in background sessions (no human to clarify). `restart_markers` is weaker in backgrounds (less linguistic mode). `tokens_per_progress` and `tool_error_count` are *more* important for backgrounds (silent thrashing is the canonical bad outcome). Phase 2 cascade controller applies different weight overrides per source (§ 4.3 `cascade.user_facing.struggle_weights` vs `cascade.background.struggle_weights`).

**Features (all pure Python/TypeScript, observable from existing event stream):**

| Feature | Source | Weight (initial) | Notes |
|---|---|---|---|
| `tool_error_count` | tool_result hook | 0.25 | Hard signal — failed tool calls are unambiguous |
| `tool_retry_count` | tool_call hook with same name+args within turn | 0.20 | Retry of same tool in same turn = thrashing |
| `tokens_per_progress` | turn_end token count / tool-progress count | 0.15 | High = model spinning, not advancing |
| `plan_drift_distance` | edit-distance vs. turn-1 stated plan | 0.10 | Cheap substring check is fine for v1 |
| `restart_markers` | regex match against "let me try", "different approach", "actually, wait" | 0.10 | Robust because models genuinely use these phrases when struggling |
| `clarification_loops` | user message contains "no, I meant" / "that's not what I asked" | 0.15 | Strong signal — user explicitly correcting |
| `tool_count_per_turn` | sum of `content[].type==="tool_use"` + top-level `tool_calls[]` + `function_call` | 0.15 | Added 2026-06-06. Payload-shape-tolerant fallback — catches struggle on raw tool-call volume even when `is_error` isn't surfaced. Saturation = 8 calls. Default sum lifted 0.85 → 1.00 deliberately (see StruggleDetector.ts comment block on `DEFAULT_STRUGGLE_WEIGHTS`). |
| `tool_timeout_rate` | tool_result duration vs. expected | 0.05 | Weak but cheap |

**Output schema:**

```typescript
interface StruggleSignal {
  session_id: string;
  bot_id: string;
  turn_index: number;
  score: number;              // [0, 1], weighted sum of features
  features: Record<string, number>;  // per-feature contribution
  current_tier: "tier0" | "tier1" | "tier2" | "tier3";
  timestamp: string;          // ISO-8601
}
```

**Non-features (explicitly excluded for v1):**

- Model self-confidence (no access on hosted APIs)
- Embedding similarity to known-struggle corpus (no labeled corpus yet)
- Inter-turn coherence (interesting but expensive; revisit Phase 4)

**Calibration:** Initial weights are educated guesses. Phase 1 telemetry produces the data to tune them; Phase 4 audit layer proposes adjustments. The weights live in `network.json` (see § 4) — not hardcoded.

**Payload sourcing — the OC hook reality:** OC does not emit tool-call outcomes through any hook (confirmed via 2026-05-26 audit, § 6). The struggle detector hooks `agent_end` to learn *when* a turn ended, then reads `/Users/<bot>/.openclaw/workspace/memory/turns-<date>.jsonl` for the rich payload (tool calls, errors, completion reason). This is the same pattern `packages/analyzer/cost_event_converter.py` already uses for cost data — proven, not novel. Lag from turn-completion to struggle-signal-available is filesystem-bound (single-digit ms in practice); cascade controller treats the signal as "best available" and falls through to current tier if not yet computed at next-turn time.

### 2.2 Component: Cascade Controller

**Lives in:** `packages/plugin/src/observer/CascadeController.ts` (new)
**Hooks:** OC's `before_model_resolve` (replaces `ModelRouter.resolveModelOverride` for cascade-enabled bots; `ModelRouter` remains as the spend-cap enforcer and user-override respecter)
**State:** per-session, in-memory, cleared on session end

**Branches on session source (§ 2.4).** User-facing and background sessions cascade in *opposite directions*. The controller reads `trigger_kind` once at session start and dispatches to the appropriate strategy thereafter.

**Decision logic (pseudocode):**

The controller returns both a tier and an optional `askHint` (for user-facing tier1 consent flow). The askHint, when present, is injected into the next turn's `appendSystemContext` so the bot can decide whether to ask the user. The tier itself does NOT change for the next turn — the bot must call `session.set_tier(choice="power")` to actually escalate.

```typescript
interface CascadeDecision {
  tier: Tier;
  askHint?: AskHint;
}

function chooseTier(session: SessionState, request: ModelResolveRequest): CascadeDecision {
  // Operator-set tier (via UI chip or session.set_tier MCP tool, see § 4.1)
  // always wins, bounded by per-bot ceiling. User choice is sticky for the
  // session — they explicitly asked, don't second-guess them.
  if (session.userRequestedTier) {
    return { tier: min(session.userRequestedTier, perBotCeiling) };
  }

  // Hard spend-cap override — existing ModelRouter behavior preserved
  if (spendCapTripped(session.bot_id)) return { tier: "tier3" };

  // Branch by session source — see § 2.4 for the rule and rationale.
  if (session.triggerKind === "user_turn" || isUserFacingSubagent(session)) {
    return chooseTierUserFacing(session);
  }
  return chooseTierBackground(session);
}

// ── User-facing: default Sonnet, demote to Haiku on triviality ──────────────
//
// Tier1 (Opus) is NEVER reached autonomously here. The cost differential
// vs tier2 is large enough (~5-10x) that we require explicit user consent —
// either via the UI chip (already shipped) or via a bot-initiated ask
// ("this is harder than expected, want me to bring in our deepest model?").
// The cascade controller's role for tier1 is to detect "tier2 is drowning"
// and surface a hint to the bot via system-context injection. The bot
// chooses whether and how to ask the user. If the user agrees, the bot
// calls session.set_tier(choice="power") and the existing ModelRouter
// precedence carries it from there. See § 2.4 (asymmetric tier transitions).
function chooseTierUserFacing(session: SessionState): {tier: Tier, askHint?: AskHint} {
  // Turn 1: start at the user-facing default (tier2 unless per-bot override).
  if (session.turnIndex === 0) {
    return { tier: session.config.userFacing.default_tier };  // typically tier2
  }

  const lastStruggle = session.lastStruggleSignal;
  const lastTriviality = session.lastTrivialitySignal;
  const recentAvg = session.recentStruggleAverage;  // last 3 turns

  // tier2 sustained struggle → inject ask-hint, do NOT change tier.
  // "Sustained" means N consecutive turns above threshold (default N=2).
  // The hint is suppressed for `tier1_ask_cooldown_turns` after a "no"
  // or no-response, to avoid nagging.
  if (session.currentTier === "tier2"
      && session.config.userFacing.tier1_ask_enabled
      && session.persistentStruggleTurns >= session.config.userFacing.tier2_struggle_persistence
      && !session.askHintCooldownActive) {
    return {
      tier: "tier2",  // no change
      askHint: {
        kind: "consider_tier1_escalation",
        struggle_features: lastStruggle.features,
        struggle_raw: lastStruggle.raw,
        turns_struggling: session.persistentStruggleTurns,
        // Bot uses this to craft a natural-language ask. Cascade does
        // NOT prescribe the wording — bot decides based on its persona
        // and the conversation context.
      },
    };
  }

  // Demote to tier3 when work is clearly trivial AND not struggling.
  // Requires positive evidence (high triviality, low struggle) — never
  // demote on absence-of-signal alone.
  if (session.currentTier === "tier2"
      && lastTriviality.score > session.config.userFacing.demote_threshold
      && lastStruggle.score < 0.1) {
    return { tier: "tier3" };
  }

  // Re-promote from tier3 → tier2 if a previously-trivial conversation
  // turns substantive (struggle starts to fire after we demoted).
  // Autonomous because the cost step (Haiku → Sonnet) is small enough
  // not to warrant asking.
  if (session.currentTier === "tier3"
      && lastStruggle.score > session.config.userFacing.tier3_repromote_threshold) {
    return { tier: "tier2" };
  }

  // De-escalation tier1 → tier2 with hysteresis (Phase 3) — only when
  // consent was via ask-hint (not via UI chip / explicit user choice).
  //
  // UI chip choices are sticky: the user explicitly picked Power for
  // the session and we don't second-guess that. Ask-hint consent was
  // scoped to "this hard task" — when struggle stabilizes, dropping
  // back to Standard respects the consent's intent rather than
  // contradicting it.
  //
  // Silent de-escalation (no bot announcement). Most users won't notice
  // (Opus vs Sonnet on trivia is subtle), and announcing would feel
  // fourth-wall-breaking. If the user explicitly asks "are you still
  // using the smart model?", the bot can clarify based on live tier
  // state.
  if (session.currentTier === "tier1"
      && session.consentSource === "ask_hint_agreed"
      && session.turnsAtCurrentTier >= session.config.userFacing.tier1_destabilize_turns
      && allRecentBelow(session, "struggle.score",
                        session.config.userFacing.tier1_destabilize_threshold,
                        session.config.userFacing.tier1_destabilize_turns)) {
    return { tier: "tier2" };
    // Note: consentSource is NOT cleared on de-escalation. If struggle
    // re-fires at tier2, the bot can re-ask (subject to cooldown), and
    // the cycle can repeat for the rest of the session.
  }

  return { tier: session.currentTier };
}

// AskHint shape — passed through to the next turn's appendSystemContext
// (same mechanism used today by stay-quiet directives in before_prompt_build,
// see PR #1086-era plumbing). Phrased as an off-the-record system signal,
// not user-facing text. The bot reads it and decides how / whether to ask.
interface AskHint {
  kind: "consider_tier1_escalation";
  struggle_features: Record<string, number>;
  struggle_raw: Record<string, number>;
  turns_struggling: number;
}

// ── Background: default Haiku, escalate to Sonnet on struggle ───────────────
function chooseTierBackground(session: SessionState): {tier: Tier} {
  // Turn 1: ALWAYS tier3. Not configurable per spec § 2.4 — operator-tunable
  // direction would mean every cron job becomes a cost-tuning decision the
  // operator doesn't want to make.
  if (session.turnIndex === 0) return { tier: "tier3" };

  const lastStruggle = session.lastStruggleSignal;
  const recentAvg = session.recentStruggleAverage;  // last 3 turns
  const cfg = session.config.background;

  // Escalate tier3 → tier2 on struggle.
  if (session.currentTier === "tier3"
      && lastStruggle.score > cfg.tier3_escalate_threshold) {
    emitSignal(session, "background_session_escalated", { from: "tier3", to: "tier2" });
    return { tier: "tier2" };
  }

  // Background sessions almost never reach tier1. Requires sustained
  // tier2 struggle AND bot-owner-tunable cap NOT zero. Default cap is 0
  // (tier1 disabled for background); operator opts in.
  if (session.currentTier === "tier2"
      && recentAvg > cfg.tier2_escalate_threshold
      && cfg.tier1_enabled) {
    emitSignal(session, "background_session_escalated", { from: "tier2", to: "tier1" });
    return { tier: "tier1" };
  }

  // De-escalation tier2 → tier3 with hysteresis (Phase 3).
  // After N=5 stable low-struggle turns at tier2, drop back to tier3 to
  // recover the cost savings. Hysteresis: the destabilize threshold is
  // much lower than the escalation threshold (0.2 vs 0.6 default), so we
  // don't oscillate. Sessions where struggle comes-and-goes stay
  // escalated; sessions that truly got past the hard part recover.
  if (session.currentTier === "tier2"
      && session.turnsAtCurrentTier >= cfg.tier2_destabilize_turns
      && allRecentBelow(session, "struggle.score", cfg.tier2_destabilize_threshold, cfg.tier2_destabilize_turns)
      && recentAvg < cfg.tier2_destabilize_threshold) {
    emitSignal(session, "background_session_deescalated", { from: "tier2", to: "tier3" });
    return { tier: "tier3" };
  }

  // De-escalation tier1 → tier2: same hysteresis pattern. Rare path
  // (requires tier1_enabled bot AND a session that climbed all the way).
  if (session.currentTier === "tier1"
      && session.turnsAtCurrentTier >= cfg.tier1_destabilize_turns
      && allRecentBelow(session, "struggle.score", cfg.tier1_destabilize_threshold, cfg.tier1_destabilize_turns)) {
    emitSignal(session, "background_session_deescalated", { from: "tier1", to: "tier2" });
    return { tier: "tier2" };
  }

  // Sustained struggle on the maximum reachable tier → emit signal so the
  // operator can investigate. Does NOT change tier (already at max).
  if (recentAvg > cfg.persistent_struggle_threshold) {
    emitSignal(session, "background_session_struggling", { tier: session.currentTier });
  }

  return { tier: session.currentTier };
}
```

**Invariants:**

- **Source determines direction.** User turns: demote-on-triviality (cascade *down* from tier2). Background turns: escalate-on-struggle (cascade *up* from tier3). Not user-configurable. See § 2.4 for the rule.
- **Background turn 1 is always tier3.** No exceptions, no per-bot escape hatch on the cascade *direction*. Per-bot config can move the ceiling (max tier reachable via escalation) but not the floor or direction.
- **Tier1 is never reached autonomously in user-facing sessions.** Only via explicit user consent — UI chip choice OR bot-initiated ask that the user agrees to. The cascade controller surfaces a hint when struggle warrants asking; the bot owns the user-facing ask. See § 2.4 (asymmetric tier transitions).
- **Tier1 in background sessions requires operator opt-in** via per-bot `background.tier1_enabled: true`. The opt-in IS the consent — no per-session ask, no human in the loop to ask.
- **User-set tier still wins everywhere** via the existing `setUserTier()` precedence in `ModelRouter`. Choice applies to user sessions where the chip / MCP tool exists; backgrounds never see it.
- **User-facing demotion requires positive evidence**, not absence-of-signal. Never demote on a no-tools turn just because struggle was 0 — that could be a complex pure-reasoning question.
- **Background struggle escalation emits a Signal** alongside the escalation, so the operator sees that compute is being thrown at a struggling background. Per-bot daily_cap_usd is the safety net of last resort.
- **Spend-cap fallthrough.** If `daily_cap_usd` tripped, cascade is bypassed and tier3 forced for any session. Matches existing `ModelRouter` behavior.
- **Ask-hint cooldown.** Once a tier1 ask is injected and the user does not agree (no `session.set_tier("power")` call within N turns, default N=3), suppress the hint for `tier1_ask_cooldown_turns` (default 10) to avoid nagging. Re-arm only on a fresh struggle re-firing after stable turns.
- **Fail-open.** Any controller error returns `{}` to OC and lets the default model resolution proceed.

### 2.3 Component: Audit Layer

**Lives in:** `packages/analyzer/audit/cascade_audit.py` (new)
**Lives in:** `packages/analyzer/cascade/` (multiple files — see component breakdown below)
**Cadence:** Daily/weekly batch jobs via existing infra-jobs framework (online tuning is out-of-scope for v1)
**Cost target:** under $0.50/day across pod (Haiku-only judging, sampled sessions)

The audit layer is not a single Haiku-judges-the-cascade pass — it's a **real learning loop** with six distinct components. The first-pass spec collapsed all of these into one job and treated the Haiku verdict as ground truth. The 2026-05-26 stress-test review (rounds 1 + 2) correctly flagged that "Haiku verdict is a feature, not a label" AND that "the learning loop must train against a counterfactual holdout, not against its own decisions." This subsection respecifies the architecture per both rounds of review.

#### Architecture

```
┌─ Phase 1/2 telemetry (already collected) ────────────────┐
│  Opik spans: tier_used, tier_intended, struggle.*,       │
│  triviality.*, ask-hint events, consent_source,          │
│  set_tier calls, trigger_kind, holdout flag              │
└───────────────────────────────────────┬──────────────────┘
                                        │
              ┌─────────────────────────┴──────────────────────────┐
              │                                                     │
              ▼                                                     ▼
   ┌─────────────────────┐                        ┌──────────────────────────┐
   │ Holdout cohort      │                        │ Labeler (daily)          │
   │ (2% pinned to       │  uncontaminated        │ Scans spans, emits       │
   │  baseline policy)   │  reference signals     │ LabeledOutcomes from     │
   │                     │ ─────────────────────► │ 5 ground-truth signals   │
   └─────────────────────┘                        └────────────┬─────────────┘
                                                               │
                       ┌───────────────────────────────────────┼─────────────────────┐
                       │                                       │                      │
                       ▼                                       ▼                      ▼
            ┌──────────────────┐         ┌──────────────────────────┐    ┌─────────────────────┐
            │ Variant          │         │ Bayesian weight tuner    │    │ Per-bot calibrator  │
            │ generator        │         │ (offline, weekly)        │    │ (offline, weekly)   │
            │ (A/B shadow arms)│         │ ±20% from ORIGINAL       │    │ 100/250 event       │
            │                  │         │ default; composition     │    │ floors              │
            │                  │         │ safety check             │    │                     │
            └──────┬───────────┘         └────────────┬─────────────┘    └─────────┬───────────┘
                   │                                  │                             │
                   └──────────────────────────────────┴─────────────────────────────┘
                                                      │
                                                      ▼
                                 ┌─────────────────────────────────────┐
                                 │ Proposal emitter                    │
                                 │ Routes through arbiter — same       │
                                 │ approval flow as everything else.   │
                                 │ Intent-respecting per memory rule.  │
                                 └─────────────────────────────────────┘
```

#### Ground truth signals

Without explicit user labels, we triangulate from five behavioral signals. The Haiku judge is a *feature*, not a label.

**Strong signals (load-bearing):**

1. **User UI-chip override events.** A user pulling Auto → Power mid-session is the cleanest possible "cascade got this wrong, should have escalated" label. Conversely, Auto → Fast mid-session is "cascade left me on Sonnet for something trivial." Sparse but high-fidelity — gold standard.
2. **`session.set_tier("power")` calls following an ask-hint.** Already classified server-side via `consent_source`. An ask-hint that gets agreed to is a labeled-positive ("cascade was right to ask"). An ask-hint that times out is a labeled-negative.
3. **Sustained struggle that resolves only at the next tier.** If a session sat at tier2 for 4 turns with struggle > 0.6, escalated to tier1, and within 2 turns struggle dropped below 0.2 — that's "escalation was correct" evidence. Available from spans alone, no judge needed.

**Weak/noisy signals (corroborative):**

4. **Cost-incident correlation.** Sessions immediately preceding a `cost_burst` Signal got *something* wrong (often upstream).
5. **Haiku judge verdict.** Still useful — but as one feature among several, not the closer. Spec § 2.3 (the prior version) treated it as ground truth; that was wrong.

**Explicitly not ground truth:**
- The Haiku verdict alone
- Session abandonment (confounded with "user got their answer and left")
- Aggregate per-day cost (too coarse)

#### Component 1: Labeler

Daily Python job. Reads yesterday's spans, applies the five signals above, emits `LabeledOutcome` records to `{shared_dir}/cascade/labels/<YYYY-MM-DD>.jsonl`:

```json
{
  "session_id": "...",
  "bot_id": "team-bot-a",
  "label": "should_have_escalated" | "should_have_demoted" | "correctly_held" | "correctly_escalated" | "correctly_demoted",
  "confidence": 0.0-1.0,
  "source": "ui_chip_override" | "ask_hint_agreed" | "struggle_resolution" | "haiku_judge",
  "evidence": { ...span IDs and feature values that drove the label... }
}
```

When multiple signals fire for the same session, all are recorded — Phase 4 tuner can weight them.

#### Component 2: Variant generator (A/B exploration)

For each pod, maintain up to 4 active *shadow variants* of the cascade thresholds. Variants are parameter perturbations (e.g., `tier3_escalate_threshold = 0.6 ± 0.05`).

**Mechanism:**

- **Production (variant A)** makes all live routing decisions. Spans tag `cascade.variant: A` AS THE FIRST FIELD WRITTEN — if span emission fails mid-write, untagged spans are dropped, not assigned to A. `cascade_variant_untagged` Signal fires if untagged-rate exceeds 1% in a rolling window.
- **Variants B-D** are shadow-only. They consume the same struggle/triviality features and emit "what would have been decided" as disagreement records. NO extra model calls — pure compute over already-emitted features.
- **Critical constraint (round-2 cost F7, tuning F3):** the Bayesian weight tuner (Component 3) consumes LabeledOutcomes ONLY from variant A (production) and the holdout cohort (Component 5). Variants B-D produce *disagreement metrics* used by the promotion gate, NOT training data for the tuner. Shadow-variant counterfactuals are inherently noisier (no real tier change happened) and would contaminate the tuner's posterior if mixed in.
- **Promotion gate** (rolling 14-day window): variant becomes the new A if ALL of: (a) ≥50 labeled outcomes where the shadow's verdict can be evaluated against actual outcomes (using struggle-resolution-after-A's-decision as the comparison signal), (b) ≥3pp improvement in misclassification rate vs A with 95% bootstrap confidence, (c) no Signal-firing regression on cost or sustained-struggle. Goes through arbiter approval — never silent.
- **Bandit retirement.** Thompson sampling over variant posteriors lets us retire clearly-losing arms early instead of waiting full 14 days. **Minimum-exploration floor:** a variant runs ≥50 outcomes before bandit retirement is allowed. **Retirement confidence gate:** posterior `P(variant < A) > 0.9` required to retire, not just point estimate. Prevents the round-2 tuning-F4 failure where a genuinely better variant is retired due to 20-outcome noise.

This is essentially what Pod-Admin originally brainstormed as "A/B + Haiku judge" but with labeled outcomes from real signals rather than Haiku-as-truth.

#### Component 3: Bayesian weight tuner

Weekly offline job. Treats each StruggleDetector feature weight as a parameter with a Bayesian prior. Updates the posterior using LabeledOutcomes from the **holdout cohort and variant A only** (see Component 6 below — never from shadow variants, never contaminated by cascade's own decisions). Proposes weight adjustments via the arbiter when posterior shifts significantly.

**Hard limits — band is anchored to ORIGINAL default, not current value:**

- Each weight has an **`original_default`** value (frozen at spec § 2.1, stored as immutable provenance in `{shared_dir}/cascade/originals.json` at first deploy) and an `auto_tune_band` of ±20% from `original_default`. Tuner CANNOT propose outside the absolute envelope `[original_default × 0.80, original_default × 1.20]`.
- This prevents the round-2 cost-F3 / tuning-F7 failure: week-over-week tuning compounding into 50%+ drift over months while every individual proposal is "in band relative to current."
- Per-cycle change cap: tuner can propose at most 5% absolute change from current value per weekly cycle, regardless of where current sits in the band. Prevents discontinuous jumps when bands widen at phase boundaries (Phase 3 → Phase 4).
- Outside-band situations emit `cascade_calibration_saturated` Signal at WARNING.
- After 3 consecutive cycles saturated in the same direction, escalate to `cascade_widen_band_proposal` — emit a separate Proposal to the arbiter asking the operator to authorize a new `original_default` value, rather than silently capping. Without this, saturated weights become invisible dead ends.
- Direction-rule invariants (§ 2.4) are NOT tunable. Tuner cannot propose flipping cascade direction per source.

**Compositional safety check:**

Per round-2 failure F3: tuning multiple weights simultaneously can compound into >40% effective threshold shift even when no single weight moved >20%. Defense:

- Each Proposal includes a **re-simulation of the prior 7 days of labeled outcomes** under the new weight vector. If any threshold's effective firing rate (escalate, demote, ask-hint) shifts by more than 15% in the simulation, the Proposal is rejected before arbiter review with `cascade_proposal_compositional_unsafe` Signal.
- Operator can override the rejection if they understand the shift (manual arbiter approval after reviewing the simulation report). The rejection is a guard, not a hard block.

#### Component 4: Per-bot calibrator

Weekly. Per-bot tuning kicks in only after pod-default convergence:

- Bot needs >100 labeled outcomes before any per-bot proposal can fire.
- Bot's misclassification rate must deviate from pod median by ≥5pp with 95% confidence.
- Per-bot *weight* overrides require >250 labeled outcomes (weights are harder to disentangle than thresholds).
- Per-bot proposals auto-populate `tiers.json::cascade.rationale` with provenance ("auto-tuned 2026-XX-XX based on N outcomes; previous: ...").

**Per-bot values are deltas from pod `original_default`, NOT new anchors (round-3 finding #3):** when a per-bot calibrator proposes a value of e.g. +10% from pod-default, the band envelope `[original × 0.80, original × 1.20]` still applies — per-bot doesn't get its own widened band. Phase 4's band-widening (Phase 3 = ±10%, Phase 4 = ±20%) applies the same envelope to per-bot values. Per-bot values existing outside the new envelope at a phase boundary get a one-time `cascade_phase_boundary_regrandfathered` Signal and stay put until next tuning cycle (no auto-snap-back, which would be visible to users as a behavior change).

#### Component 5: Holdout cohort (the un-contaminated reference)

The single most important addition from round-2 review (tuning F1, F2, F3, F8 all share this root cause). Without a holdout, the learning loop trains on cascade's own decisions and becomes self-confirming.

**Mechanism:**

- A fixed percentage of sessions (default 2%, configurable 1-5%) are deterministically assigned to the **holdout cohort** at session start.
- Holdout cohort sessions are NOT routed by cascade. They run on the original keyword classifier's tier choice (during Phase 3 transition) or a pre-Phase-2 baseline (e.g., always tier2 for user_turn, always tier3 for background — the conservative default).
- Holdout cohort sessions are tagged in spans with `cascade.holdout: true` and otherwise emit the full struggle / triviality / cost telemetry.

**Why this works:**

- LabeledOutcomes from holdout sessions show what struggle distribution looks like under the *baseline* policy, uninfluenced by cascade decisions. This is the only un-contaminated ground truth available to the system.
- Per failure F8 (Haiku-judge circular dependency): the Sonnet sanity check runs against holdout sessions, not cascade-affected ones. Drift in the cascade-affected distribution doesn't fool the drift detector.
- Per tuning F1 (struggle-resolution self-confirming): the tuner compares cascade-affected outcomes against holdout outcomes. "Did escalating to tier1 actually outperform holdout's stay-on-tier2?" is now a falsifiable question.
- Per tuning F2 (adversarial UI-chip): the tuner weights holdout-derived signal MORE than cascade-affected signal when there's disagreement.

**Selection determinism:**

- Cohort assignment is by hash of `(bot_id, session_id)` against the configured percentage. Same session always same assignment — restart-safe, idempotent.
- Per-bot opt-out: bots can set `cascade.holdout.enabled: false` for production-critical sessions where the baseline policy is meaningfully worse (e.g., a bot whose work genuinely requires Sonnet and where Haiku-baseline would degrade output the user sees).

**Cost ceiling:**

- 2% of sessions running on baseline policy adds at most 2% to overall cost (and probably less, since baseline policy may be cheaper for many session types).
- Hard limit: holdout cohort percentage cannot exceed 5% pod-wide. Override requires direct config edit + a `cascade_holdout_oversized` Signal on every cycle that the override is active.

**What's NOT in the holdout:**

- User-explicit UI-chip choices: even if a session is in the holdout cohort, an explicit user pick (Power, Standard, Fast) wins. The user's contract with the bot trumps the experimental cohort.
- Spend-cap forced demotions: holdout sessions still respect spend-cap.
- Cost-management: runaway-rate hard cap still applies to holdout sessions (the experiment isn't worth a runaway cost burn).

**Hijacked-holdout handling (round-3 finding #1):**

When a UI-chip / set_tier call overrides a holdout-assigned session, the span is tagged `cascade.holdout_hijacked: true` (NOT `cascade.holdout: true`). The tuner excludes hijacked sessions entirely — they're contaminated data, not "cascade-affected" or "baseline" cleanly. If the effective baseline cohort rate drops below `holdout_target × 0.7` (e.g., target 2%, effective <1.4%) for a rolling 24h window, emit `cascade_holdout_undersized` Signal — the cohort sizing logic targets *post-hijack* rate, not raw assignment, so this prompts an automatic bump.

**Subagent holdout assignment (round-3 finding #6):**

Subagents inherit the parent's holdout assignment — NOT independently hashed. Otherwise a holdout-assigned parent spawning 7 subagents would have some children running cascade and some baseline, making the parent's outcome incoherent (parent's struggle distribution shaped by children's cascade decisions). Subagent spans tag `cascade.holdout_root_session_id: <parent_session_id>` so the tuner can group correctly.

**Span variant tagging on holdout (round-3 finding #5):**

Holdout sessions carry `cascade.variant: "baseline"` (distinct value, not `"A"`). The tuner's training query is `variant IN ('A', 'baseline')` with weighting governed by Component 5's "weight holdout-derived signal more on disagreement" rule. This avoids contaminating the variant-A LabeledOutcome stream with non-cascade-decided data.

**`tier_intended` on holdout sessions (round-3 finding #2):**

For holdout sessions, `tier_intended` = cascade's *shadow verdict* (computed but not applied), `tier_used` = baseline's actual choice. Holdout spans additionally carry `cascade.shadow_verdict_computed: true`. This is the whole point of the holdout — the divergence between cascade's intent and baseline's truth IS the counterfactual signal the tuner needs.

This is a small (1-5%) but structurally critical addition. It transforms the learning loop from "training on its own decisions" to "training with a real counterfactual reference." No statistical magic required — just preserve a slice of un-cascaded sessions and compare.

#### Component 6: Proposal emitter

All proposals from components 2-4 route through the existing arbiter approval flow. **No silent auto-application, ever.** The audit layer is "automated drafting" of cascade-tuning Proposals, not "automated tuning."

Per [feedback_generators_consider_intent](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_generators_consider_intent.md), the proposal emitter checks for explanatory operator events before proposing changes:

- If the bot's `tiers.json` was edited within the look-back window → don't propose contrary changes; emit `cascade_intent_conflict` Signal instead
- If a new tool was added to the bot, or a manifest changed → check whether the new struggle distribution is explained by the tool change; if yes, surface that hypothesis in the Proposal rather than blindly retuning
- If the OC version changed in the look-back → defer tuning proposals until N days of post-version data have accumulated

This applies the existing "consider intent" rule to the learning loop itself, preventing the same failure pattern that `auth_drift_filler` produced (PR #1430).

#### Cost ceiling

- Audit hard cap: $0.50/day pod-wide (Haiku judge calls only). Already in spec; kept.
- Shadow variants add **zero LLM cost** — pure compute over already-emitted features. This is what makes the A/B mechanism feasible.
- Weight tuner + calibrator + emitter are all pure Python, run weekly. Negligible cost.

#### Phase boundaries

- **Phase 2:** Telemetry emission for variant comparison spans (2 initial variants — default A + one perturbation along `tier3_escalate_threshold`). Label inference job runs advisory-only, writes LabeledOutcomes for analysis. No tuning proposals fire yet.
- **Phase 3:** Tuner + calibrator + emitter all go live with conservative initial bands (10% not 20% from default). Proposals subject to arbiter approval. Audit-of-audit (5% Sonnet sanity check on Haiku judge verdicts) lands here.
- **Phase 4:** Bands widen to 20% from default. Variant generator allowed to propose new perturbations along axes the data justifies. Per-bot calibration enabled.

This is a real learning loop with concrete falsifiable hypotheses about what "better" means, no statistical fanciness that would be hard to debug six months from now.

### 2.4 Session source and direction asymmetry

The single most important design decision in the cascade controller: **user-facing and background sessions cascade in opposite directions, and this is a hard rule, not user-configurable.**

#### The rule

| Session source | Default tier (turn 1) | Cascade direction | Tier1 reachability |
|---|---|---|---|
| `user_turn` | **tier2** (Sonnet) | Demote to tier3 on triviality, escalate to tier1 on rare struggle | Via UI chip / `session.set_tier`, or rare struggle-driven escalation |
| `heartbeat`, `cron_app` | **tier3** (Haiku) | Escalate to tier2 on struggle | Default off; bot-owner opt-in via per-bot `tier1_enabled: true` |
| `subagent` | Inherits from parent session's trigger_kind | Inherits | Inherits |

**Where `trigger_kind` comes from.** Already a known concept in the codebase — `packages/analyzer/cost_event_converter.py:_infer_trigger_kind()` maps OC's `source` / `channel` fields to `user_turn | heartbeat | cron_app | subagent | summarizer | ...`. Cascade reads it from the agent_end ctx at session start.

#### Why the asymmetry

**User turns: default tier2 (Sonnet), demote when work is clearly trivial.**

- *First impressions matter.* The first turn shapes the whole session. Starting on Haiku and switching to Sonnet at turn 4 is jarring UX; starting on Sonnet and silently demoting on turn 2 when the work was clearly trivial feels like the bot "got snappier" — a perceptual win.
- *Cascade can't predict difficulty upfront.* That's the problem we abandoned. Starting from the high-quality side and demoting on observed evidence sidesteps the prediction problem.
- *Most user sessions are short.* The savings from defaulting Haiku are tiny if the session is 2 turns and one of them needed Sonnet. The downside (a bad first turn that shapes the rest of the conversation) is large.
- *Demotion requires positive evidence* (high triviality signal + low struggle signal), never absence-of-signal alone. A no-tools turn with low struggle could be a complex reasoning question — don't demote on it.

**Background turns: default tier3 (Haiku), escalate when struggle is real.**

- *No latency cost.* Nothing perceptual is lost by escalating mid-session.
- *Predictable cost floor.* Defaulting tier3 means a runaway background loop has a natural cost ceiling. Silent expensive-by-default backgrounds are exactly the failure family we're guarding against (per the internal cost-alerting-blackout postmortem).
- *Most background work is genuinely simple.* Email scans, calendar checks, audit sweeps. Sonnet would be over-provisioned by default.
- *Reversibility costs nothing.* A failed background turn on Haiku that escalates and succeeds on Sonnet has zero user-visible impact.
- *Different failure mode.* Backgrounds have no human to notice and complain. Defaulting expensive means expensive failure can run unnoticed for a long time. Defaulting cheap caps that risk.

#### Asymmetric treatment of tier transitions — by cost step

Cascade transitions are NOT all treated the same way. The cost ratio between adjacent tiers governs whether escalation is autonomous or consent-required:

| Transition | Direction | Cost ratio (rough) | Treatment |
|---|---|---|---|
| **User-facing transitions** | | | |
| tier3 → tier2 (Haiku → Sonnet) | escalation (re-promote) | ~3× | **Autonomous, struggle-driven.** The cost step is small enough that the round-trip of asking the user costs more than just doing it. |
| tier2 → tier3 (Sonnet → Haiku) | de-escalation (demote) | savings | **Autonomous, triviality-driven.** Demotion only saves money; no consent needed. Requires positive evidence (high triviality + low struggle). |
| tier2 → tier1 (Sonnet → Opus) | escalation | ~5-10× | **Bot asks user for consent.** Cascade detects sustained struggle, injects an `AskHint` via `appendSystemContext`. Bot decides whether and how to ask. If user agrees, bot calls `session.set_tier(choice="power", consent_source="ask_hint_agreed")` and `ModelRouter` carries it from there. |
| tier1 → tier2 (Opus → Sonnet) | de-escalation | savings | **Autonomous, but conditional on consent source.** If consent was via `ask_hint_agreed` AND struggle stabilizes for N turns at tier1, drop back silently. If consent was via UI chip (`ui_chip`), STICKY — no auto de-escalation, user explicitly chose Power. Phase 3 deliverable. |
| anywhere via UI chip | n/a | n/a | **Sticky for the session.** User explicitly chose; no autonomous changes (no asks, no demotes, no de-escalations). |
| **Background transitions** | | | |
| Background tier3 → tier2 | escalation | ~3× | Autonomous + emits `background_session_escalated` Signal so operator sees compute climbing. |
| Background tier2 → tier3 | de-escalation | savings | **Autonomous with hysteresis.** After N=5 stable low-struggle turns at tier2, drop back. The destabilize threshold (~0.2) is much lower than the escalation threshold (~0.6), so transient-struggle sessions stay escalated; truly-recovered sessions free up the cost. Phase 3 deliverable. |
| Background tier2 → tier1 | escalation | ~5-10× | **Operator pre-consent** via per-bot `tier1_enabled: true`. No per-session ask (no human to ask). Defaults to disabled. |
| Background tier1 → tier2 | de-escalation | savings | **Autonomous with hysteresis** when bot is tier1-enabled. Same shape as tier2→tier3 background de-escalation. Phase 3 deliverable. |

**Why this asymmetry is the right design:**

- *Cost transparency matters at large deltas.* A surprise 5-10× bill is a trust violation. A 3× difference is invisible to most users (they're not looking at per-turn cost) and not worth interrupting the conversation over.
- *Asking creates a natural pause point.* The bot can articulate *why* it thinks the task is hard ("the search tool keeps failing — want me to try with our deepest model that might find a different approach?"). This is also useful UX feedback about how things are going.
- *Background sessions have no one to ask*, so the operator's per-bot opt-in is the equivalent of the user's "yes." Pre-consenting via config is the only coherent option.
- *Autonomous tier1 was a cost trap.* The earlier version of this spec had `chooseTierUserFacing` auto-escalate to tier1 on struggle. That would have meant: any session that hit the threshold (possibly due to a flaky tool or a single bad turn, not a real model-capability gap) would burn Opus rates for the rest of the session. Removed 2026-05-26 after design review.

The bot's ask is mediated by the existing `session.set_tier` MCP tool — no new escalation primitive. Cascade only signals; the bot only asks; the user decides; the existing user-tier precedence in `ModelRouter` carries the result.

#### Not user-configurable for backgrounds — by design

The UI tier chip (Auto/Fast/Standard/Power) per PR #1629 / [spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md) applies only to user-driven turns where a chip exists. Backgrounds never see the chip — there's no turn-start UI affordance for them. When a user picks "Power" they mean **for their conversation right now**, not "make this bot's nightly audit jobs use Opus."

Per-bot config can:
- Move the ceiling (`background.tier1_enabled: true` opts a bot into background tier1 escalation; default off)
- Set struggle thresholds (`background.tier3_escalate_threshold`)
- Disable cascade entirely (`enabled: false`) — for bots that should never cascade

Per-bot config cannot:
- Flip the direction (background → tier2-default, user → tier3-default)
- Disable the floor for backgrounds (heartbeats must start tier3)

#### Subagent and cross-bot dispatch — explicit propagation rules

The first-pass spec hand-waved subagent inheritance ("inherits from parent") and didn't address cross-bot dispatch at all. The cost stress-test review (F2 + F7) found both could fan a single user tier-1 choice into 5-10× the cost the user actually consented to.

**Subagent rule (same-bot, parent-spawned child sessions):**

| Parent tier | Parent consent | Subagent default tier | Subagent tier1 reachable? |
|---|---|---|---|
| tier3 | any | tier3 | No (background rules) |
| tier2 | autonomous (classifier) | tier3 (one step DOWN) | No |
| tier2 | `ui_chip`, `ask_hint_agreed`, `bot_initiated` | tier2 (parity) | Only via cascade's own ask-hint flow within the subagent — NOT inherited from parent |
| tier1 | `ui_chip` | tier2 (CAP — one step down) | No autonomous; subagent's own ask-hint flow required |
| tier1 | `ask_hint_agreed`, `bot_initiated` | tier2 (CAP) | No autonomous; subagent's own ask-hint flow required |

Key principle: **tier1 consent does NOT propagate to subagents.** The user consented to tier1 for their conversation with the parent bot, not for an unbounded subagent fan-out. Subagents start one tier below the parent's tier1 (i.e., at tier2) and must earn their own escalation through cascade signals. This bounds the multiplier: a single parent tier1 session spawning K subagents costs at most parent-cost + K × tier2-cost, not parent-cost + K × tier1-cost.

Per-bot config can override this if the operator explicitly wants subagent tier1 inheritance (`cascade.subagent_inherit_tier1: true`), but the default is no.

**Cross-bot dispatch rule (evo → team-bot-a, etc.):**

Cross-bot dispatch (evo dispatches a task to another bot via the existing cross-bot mechanism — memory `project_evo_oc_native_architecture`) is **NOT** subagent inheritance. The receiving bot's session is a fresh session under that bot's own cascade config.

| Source | Receiving bot starts at |
|---|---|
| Evo session at any tier, `consent_source = "ui_chip"` for Power | Receiving bot starts at ITS default for user_turn (tier2). Evo can pass `tier_hint: "power"` which receiving bot's cascade controller MAY honor IF the receiving bot's per-bot `cascade.honor_cross_bot_tier_hint: true` (default false). |
| Evo session at tier3 | Receiving bot starts at default for user_turn (tier2). No carryover. |
| Any cross-bot dispatch | Receiving bot's spans tag `cascade.dispatched_from_bot: <evo_bot_id>` for audit/lineage. |

The default-false on `honor_cross_bot_tier_hint` means: by default, a user picking Power on evo does NOT propagate Power costs to dispatched bots. The operator explicitly opts a bot in if they want that. This addresses cost F7 directly.

#### Edge case: cron-but-user-visible

The morning briefing pattern — `trigger_kind = cron_app` but the output is read by the user. By the rule above it defaults Haiku, which may be wrong for output quality.

**v1 disposition:** ship the simple rule. If a specific bot's user-visible cron output degrades, the operator sets that bot's `tiers.json::cascade.background.force_default_tier: tier2` as an explicit override. We don't try to infer "is this background going to be shown to a user" from inside the cascade — that's brittle. The escape hatch is per-bot config.

If we see this become a common pattern (3+ bots needing the override), revisit and add a session-level "output_visibility" hint that the wizard / app manifest can carry.

### 2.5 Component: Triviality Detector

**Lives in:** `packages/plugin/src/observer/TrivialityDetector.ts` (new — Phase 2)
**Output:** a `TrivialitySignal` per turn, symmetrical to `StruggleSignal`
**Purpose:** the demotion signal for user-facing cascade (§ 2.2 `chooseTierUserFacing`)

Same shape as `StruggleDetector` — pure-function, no I/O, takes agent_end messages and outputs a score in [0, 1] plus per-feature contributions.

**Features (initial — Phase 4 audit may tune):**

| Feature | Weight | Saturation |
|---|---|---|
| `short_user_message` | 0.25 | < 50 tokens fully saturates |
| `single_decisive_tool` | 0.25 | one tool call, completed first-try |
| `short_assistant_response` | 0.15 | < 100 tokens fully saturates |
| `no_struggle_markers` | 0.15 | absence of all StruggleDetector regex hits |
| `no_clarification` | 0.10 | user didn't push back |
| `low_token_spend` | 0.10 | total tokens < per-bot baseline |

**Demotion only when:**
- `triviality.score > userFacing.demote_threshold` (default 0.7), AND
- `struggle.score < 0.1` (positive struggle absence)

Asymmetric thresholds: it takes strong positive evidence to demote, but only weak struggle to *not demote*. Skewed conservative because the cost of a wrong demote (next turn on Haiku, mediocre answer to a real question) is more visible than the cost of staying on Sonnet (small extra spend).

Phase 2 deliverable alongside the cascade controller. Phase 1 doesn't need this — Phase 1 is telemetry-only and doesn't act on cascade decisions.

### 2.6 Cost management

**Major redesign 2026-05-27.** The previous draft of this section ("Density and pressure guardrails") specified per-session hard caps on cost, turns, and wall-clock. The 2026-05-27 mini discovery script run found ~50% of active bots would trip those defaults on legitimate work — they were treating cap-tripping as the failure mode when the actual problem was elsewhere. This section is rewritten around three insights:

1. **Caps are brittle because behavior is lumpy.** A user may ignore a bot for four days, then engage heavily on day five. Average is fine; any per-session cap trips on day five anyway. Operators also don't know what reasonable cap values are at granular levels.

2. **User-initiated cost is consent.** When the user is in a Telegram session actively asking the bot to do real work, that's exactly what we want to encourage. Gating user engagement with caps treats the symptom of "we don't want runaway cost" with a mechanism that constrains the *intentional* part of cost.

3. **The dangerous cost is invisible.** Background processes running on expensive models with large contexts, where the user has no visibility until the monthly bill lands. The combination — auto-tier + tier1 + large context + background — is qualitatively different from "user is doing expensive work" and deserves a qualitatively different response.

The cascade decisions in § 2.2 still need protection from aggregate runaway behavior. But the mechanism is *budget pacing + anomaly detection + a single rate-limit safety net*, not per-session caps.

This section specifies cost-management surfaces that complement the cascade decisions. The internal cost-alerting-blackout postmortem remains the canonical motivating incident — but the design that prevents its recurrence isn't caps; it's the anomaly-classifier + dangerous-combo detector below.

#### Budget pacing (advisory, operator-facing)

Per-bot monthly budget. Operator-set, optional. Default = no budget = unconstrained.

**Auto-suggestion via evo (recommended default):** after 30 days of cost data for a bot, evo proactively asks the operator: "team-bot-a spent $42 last month — want me to set $60 as the budget?" The 1.4× buffer accounts for normal variance. If the operator agrees, the budget lands in `tiers.json::cascade.cost_management.budget_usd_per_month`. Operator can change anytime via evo or admin UI.

**Pacing tracker:** at any moment in the period, compute "projected end-of-period spend at current pace vs budget." Surface as:
- Admin UI tile: current pace + projected end-of-month + budget + delta
- Signal `cascade_budget_pacing_drift` (info) when projected to exceed by >10% — operator can preempt
- Signal `cascade_budget_exceeded` (warning) when actually exceeded

**No gating.** Budget pacing is pure observation. Bots keep working past their budget. The signal exists so the operator notices before the next bill — not so the system silently rate-limits work the operator may want done.

#### Anomaly detector (origin-aware)

Rolling 30-day baseline per bot for: cost/turn, context-tokens/turn, sessions/day, tier mix (% of sessions and % of cost at each tier). Anomaly fires when current value exceeds N× baseline, where N varies by **session origin** — what kind of session produced the spike.

| Session origin | Forbearance | Inform threshold | Warn threshold | Surface |
|---|---|---|---|---|
| User-initiated chat (Telegram, admin UI) | High | 3× baseline | 10× baseline | Pacing tile only |
| UI-chip Power explicitly chosen | Highest | — | 10× baseline | Pacing tile only |
| Background w/ user-visible output (morning briefing class) | Medium | 2× baseline | 3× baseline | `cascade_anomaly_user_visible_background` |
| Background pure (cron/heartbeat) | Low | 1.5× baseline | 2× baseline | `cascade_anomaly_background` |
| Cascade-escalated tier1 (autonomous, not user-consented) | Very low | 1.2× baseline | 1.5× baseline | `cascade_anomaly_cascade_escalation` |

**Rationale for asymmetry:** user-initiated turns are *exactly* what we want to encourage. The user chose to spend; signaling at 3× baseline gives visibility without nagging. Background-pure turns are invisible to the user, so a 2× anomaly is worth the operator knowing about immediately. Cascade-escalated tier1 is the most suspicious — the system decided to spend more without user consent; even a 1.5× anomaly is worth flagging.

**Bootstrapping:** new bots have no baseline. For the first 14 days, the anomaly detector uses pod-median baselines (scaled to similar-shape bots when discoverable; otherwise the pod-wide median). After 14 days of bot-specific data, switch to bot-specific baselines.

#### Dangerous-combo detector (named pattern, single-occurrence signal)

The cost-management equivalent of "if these four things happen at once, the operator should know immediately." A single turn matching ALL FOUR fires `cascade_dangerous_combo` Signal at WARNING — no baseline comparison needed, no waiting for accumulation:

1. **Origin:** background-pure (`trigger_kind ∈ {heartbeat, cron_app}` — no human in loop)
2. **Tier:** tier1 (Opus, ~5-10× tier2 cost)
3. **Source of tier choice:** cascade-decided (NOT operator's `ui_chip` Power or `bot_initiated`)
4. **Context size:** large (>100K tokens, configurable)

The pattern that motivated this: cron job → cascade escalates because struggle signal fired → tier1 → huge context (e.g., reading all of a user's docs) → silent expensive turn the operator never sees until the bill.

Independent of the anomaly detector — fires on a single occurrence, not on baseline-relative comparison. Per the 2026-05-27 design pivot, this is the *specific* failure mode that the operator explicitly worried about; the design must surface it explicitly rather than hoping it shows up as a generic anomaly.

#### Runaway-rate hard cap (single, minimal)

One per-session hard cap remains: a $/5-minute spend-rate limiter that catches genuine runaway loops (model retrying tool calls in a thrash, plugin bug causing infinite turn generation, etc.). This is qualitatively different from a cost-control mechanism — it's a tripwire for *broken behavior*, not a ceiling on intentional work.

| Cap | Default | Trigger |
|---|---|---|
| `runaway_rate_usd_per_window` | $20 | Per-session spend exceeds this within `window_minutes` |
| `window_minutes` | 5 | The rolling window |

**When tripped:** force tier3 for remainder of session, emit `cascade_runaway_rate_tripped` Signal at WARNING (escalates to CRITICAL if it trips 3+ times in 24h). Same precedence as existing `daily_cap_usd` — operates regardless of consent source, regardless of cascade state. Catches the catastrophic case that monthly budgets can't: a single session burning $50 in 10 minutes shouldn't wait for monthly pacing to notice.

**Enforcement placement:** in `ModelRouter`, not `CascadeController`. ModelRouter always runs even when cascade is disabled. This preserves the round-3 finding #4 insight (caps that live in cascade-state risk silent disablement) without keeping the per-session cost cap that was the original symptom.

#### Pod-wide concurrency and rate limits (lightweight watchdog)

Lives in `packages/analyzer/cascade/pressure_watchdog.py` (new). Polls recent spans every 60s, writes flags to `{sharedDir}/cascade/pressure_flags.json`, expires flags on TTL. CascadeController reads flags at decision time. Same pattern as the existing spend-cap flag mechanism — proven shape, no new architecture introduced.

| Cap | Default | Action when hit |
|---|---|---|
| `max_concurrent_tier1_sessions_pod_wide` | 3 | Reject further tier1 escalations (cap at tier2); emit `pod_tier1_concurrency_cap` Signal. **Exception:** user-explicit UI-chip Power picks are NOT subject to this cap — operator's explicit choice is honored. |
| `max_escalations_per_15min_pod_wide` | 5 | Freeze all autonomous escalations for the next 15min (user-explicit picks still honored); emit `escalation_storm` Signal with the bots involved |
| `tier1_pod_spend_per_hour_usd` | $10 | Suspend autonomous tier1 escalations pod-wide until the rolling-hour window clears; emit `tier1_pod_spend_burst` Signal |

#### Cross-bot correlation

If N≥3 bots hit the same tool error pattern (same tool name, similar error string) within 5 minutes, **suppress autonomous escalations** on the affected bots and emit an `upstream_tool_outage` Signal with the correlated error pattern. Rationale: the problem is upstream, not capability — escalating to a more expensive model won't fix a Gmail rate-limit. The operator clears the Signal when the underlying issue is resolved.

#### Interaction with user choice

**User-explicit tier choices via UI chip (`consent_source = "ui_chip"`) bypass pod-wide concurrency caps.** When the operator picked Power for their conversation, they shouldn't get bumped because a cron job took a slot. The cap applies to autonomous escalations (cascade-decided or ask-hint-driven), not to explicit user picks. Spend-cap (the existing per-bot `daily_cap_usd`) still applies regardless.

Pod-wide caps **do** apply to `ask_hint_agreed` consent — the user agreed to escalate for a hard task, but if the pod is already under tier1 pressure, the bot's ask-hint should not have fired in the first place (cascade should suppress ask-hint emission when the pod is at concurrency cap). This is the most subtle case: when pod is at tier1 cap, the cascade controller does NOT emit an ask-hint, so the bot never asks, so the user never says yes, so the system stays under cap. Per-bot soft signals (`pod_tier1_concurrency_reached_at_ask_time`) record this for operator visibility.

#### Configuration

All thresholds live in `network.json::models.cascade.cost_management`. Defaults are sensible enough that most operators never touch this block.

```json
{
  "models": {
    "cascade": {
      "cost_management": {
        "budget": {
          "auto_suggest_after_days": 30,
          "auto_suggest_buffer_multiplier": 1.4,
          "pacing_drift_warn_percent": 110
        },
        "anomaly_detector": {
          "baseline_window_days": 30,
          "bootstrap_pod_median_days": 14,
          "min_baseline_observations": 50,
          "thresholds": {
            "user_initiated_inform": 3.0,
            "user_initiated_warn": 10.0,
            "ui_chip_warn": 10.0,
            "background_user_visible_inform": 2.0,
            "background_user_visible_warn": 3.0,
            "background_pure_inform": 1.5,
            "background_pure_warn": 2.0,
            "cascade_escalation_inform": 1.2,
            "cascade_escalation_warn": 1.5
          }
        },
        "dangerous_combo": {
          "enabled": true,
          "trigger_kinds": ["heartbeat", "cron_app"],
          "tier": "tier1",
          "min_context_tokens": 100000
        },
        "runaway_rate_cap": {
          "enabled": true,
          "dollars_per_window": 20.0,
          "window_minutes": 5
        },
        "pod_wide": {
          "max_concurrent_tier1_sessions": 3,
          "max_escalations_per_15min": 5,
          "tier1_pod_spend_per_hour_usd": 10.0
        },
        "cross_bot_correlation": {
          "enabled": true,
          "min_bots_for_correlation": 3,
          "window_minutes": 5
        }
      }
    }
  }
}
```

Per-bot overrides go in `tiers.json::cascade.cost_management`; any omitted field falls through to pod default. **Budget specifically is per-bot only** (`tiers.json::cascade.cost_management.budget_usd_per_month`) — pod-wide budget would be meaningless because bot roles differ wildly.

#### Existing `daily_cap_usd` interaction

Stays. The new monthly budget is a *steady-state* observation surface; `daily_cap_usd` is an *emergency* daily circuit breaker. Different timescales, different responses. A bot may pace toward monthly budget normally but have one anomalous day that trips daily_cap_usd — that's the existing safety net catching a runaway day, distinct from the new monthly pacing.

#### What got dropped from the prior draft (and why)

These were in the previous "Density and pressure guardrails" draft; the 2026-05-27 design pivot removed them:

- **`max_session_cost_usd_hard`** ($2 background, $10 user-facing) — brittle: discovery run found admin-bot's legitimate audits and evolve's user chats would routinely trip these. Replaced by monthly budget + anomaly detector + runaway-rate cap.
- **`max_session_turns_total`** (50 background, 200 user-facing) — wasn't catching any real failure mode the anomaly detector won't catch better.
- **`max_session_wall_clock_minutes`** (30 background) — false-positives confirmed empirically: admin-bot's 446-min audits and evolve's multi-hour user sessions are legitimate. No replacement needed; anomaly detector handles spike detection.
- **`max_consecutive_tier1_turns_per_session`** (5/30) — placeholder. Anomaly detector's tier-mix baseline catches "this bot is suddenly running more tier1 than usual."
- **Soft-notify at $3 + `extend_session_cost_cap` MCP tool** — the cost-cap-extension MCP tool was complexity that only existed to soften the hard cap. With no hard cap, no extension needed. Bot can still surface anomaly info conversationally via the standard inform/warn signals.

#### Watchdog reliability (load-bearing — the watchdog cannot fail silently)

Per round-2 review (cost F1, failure F1+F8, migration F1): the pressure_watchdog is load-bearing for pod-wide concurrency enforcement AND for the anomaly detector's baseline computation. If it fails silently, both vanish — exactly the failure family this section exists to prevent.

**Heartbeat contract:**

- `pressure_flags.json` always carries a top-level `"watchdog_heartbeat": "<ISO-8601 timestamp>"` field, written on every poll cycle (every 60s) regardless of whether any flags are set.
- The file also carries `"watchdog_ttl_seconds": 180` so the consumer knows how stale is too stale.
- CascadeController reads the file at decision time. If `now - watchdog_heartbeat > watchdog_ttl_seconds` (default 180s — three missed polls), treat the watchdog as dead.

**Fail-closed behavior when watchdog dead:**

When the watchdog is dead, cascade does NOT trust the flags (they may be stale). Behavior:

- All autonomous tier1 escalations capped → forced to tier2 (autonomous tier3→tier2 escalation still allowed; only tier1 is suppressed)
- All ask-hints suppressed (we can't tell if pod is at concurrency cap)
- UI-chip and bot-initiated tier1 still honored (explicit user/bot consent + runaway-rate hard cap + daily_cap_usd still in force)
- Emit `cascade_pressure_watchdog_stale` Signal at WARNING on first detection; escalate to CRITICAL after 1 hour
- Per-bot daily_cap_usd and runaway-rate hard cap continue to operate normally

**Defense against coupled telemetry-failure:**

The watchdog computes pod-wide tier1 count by reading recent spans. When telemetry is failing (per § 2.7 `cascade_telemetry_lost`), spans aren't being written, so the watchdog reads zero sessions and would conclude "no pressure" — granting every escalation in the period exactly when observability is broken. **Defense:**

- CascadeController maintains an **in-process counter** of "tier1 sessions started in this process that haven't ended yet." This is exact, not derived from spans.
- The watchdog's pod-wide count is the **maximum** of (a) what spans show, (b) what each plugin process's in-process counter reports (via a small heartbeat file `{shared_dir}/{bot_id}/cascade/tier1_active.json` written by each plugin on every tier1 grant). When telemetry fails, the in-process counters still work.
- When `cascade_telemetry_lost` Signal is active, the pod-wide concurrency cap is reduced to `max(2, default/2)` — extra conservative because we're operating partly blind.

**Deployment:**

- New launchd plist `ai.evolve.cascade.pressure-watchdog.plist` (mirrors existing daemon patterns)
- Installed by `evolve-admin install-infra-jobs --with-cascade-watchdog` (gated flag for Phase 2 rollout, default-on for Phase 3+)
- Runs as the `evolve` user (the spans dir under `{shared_dir}` already has read ACL for `evolve`)
- Pod-singleton: launchd LaunchDaemon scope ensures only one instance runs
- Owned by `monitor_coverage` — the watchdog itself is a monitored daemon, same as every other infra job. If it crashes, `monitor_coverage` fires the canonical "daemon down" Signal.

**Watchdog-of-the-watchdog escalation:**

- `cascade_pressure_watchdog_stale` at WARNING for 5+ minutes → also fire `cascade_pressure_watchdog_dead` at CRITICAL
- `cascade_pressure_watchdog_dead` at CRITICAL for 30+ minutes → page operator via the same per-bot operator-message path used by other critical Signals (see § 2.7 for the meta-pattern)

#### Signal grouping under pressure (round-3 finding #7)

A single tier1-pressure event can trigger multiple Signals simultaneously: `pod_tier1_concurrency_cap`, `escalation_storm`, `tier1_pod_spend_burst`, per-session `session_tier1_cap` instances, plus potentially `cascade_pressure_watchdog_stale` if the storm caused watchdog lag. Five Signals for one underlying condition is noise.

The watchdog assigns a **`cascade.pressure_event_id`** correlator (UUID) to all pressure-family Signals firing within a 5-minute window. Alerts page consumers (admin UI) can collapse the visual presentation by correlator while preserving each Signal's individual resolution path. Each Signal still fires (operator may resolve them independently), but the operator sees one logical event with sub-items instead of a wall of alerts.

#### Phasing

- **Phase 1 (shipped):** telemetry only. Spans carry cost, tier_used, trigger_kind, context-token sizes per turn — the baseline data the anomaly detector and dangerous-combo detector need.
- **Phase 2:**
  - **Anomaly detector** lands advisory-only (computes baseline + would-have-fired Signals, doesn't actually surface them yet — calibration period)
  - **Dangerous-combo detector** lands LIVE (single-occurrence pattern; no baseline accumulation required)
  - **Runaway-rate hard cap** lands LIVE in ModelRouter (operates independently of any baseline)
  - **Pod-wide concurrency watchdog** lands shadow-mode (flags computed, not consulted)
  - **Discovery script v2** produces baseline-distribution report (rewritten — no longer cap-trip flags; instead per-bot mean/p50/p95/max for cost-per-turn, context-tokens, sessions-per-day, tier-mix to seed the anomaly baseline)
- **Phase 3:**
  - Anomaly detector goes live (Signals fire per the thresholds table)
  - Pod-wide watchdog enforcement goes live
  - Budget pacing tile lands in admin UI
  - Cross-bot correlation lands
  - Evo's auto-suggestion flow for budgets goes live (the operator-dialogue side)
- **Phase 4:**
  - Per-operator profile learning — adjust anomaly thresholds to the specific operator's observed engagement pattern (e.g., "this operator engages heavily on weekends; weekend anomalies have higher forbearance")
  - Audit layer uses anomaly-Signal frequency as one input to per-bot threshold tuning

### 2.7 Failure semantics

Cascade adds new failure-mode surface — the components in § 2.1-2.6 can fail in ways the existing classifier didn't. This section consolidates what cascade does when things break, named after the failure modes the 2026-05-26 stress-test review identified.

#### Cold-start rehydration (plugin restart mid-session)

**Problem:** CascadeController state is in-memory (`turnsAtCurrentTier`, `persistentStruggleTurns`, `askHintCooldownActive`, `consentSource`, `recentStruggleAverage`). On plugin restart, that state is lost. An in-flight session arrives at the next `before_model_resolve` with `turnIndex > 0` but blank cascade state. Naive behavior would treat this as turn 1 (defaults to tier2 for user-facing) — silently reversing a user's explicit Power choice from before the restart.

**Required behavior:**

1. On controller startup, before serving any decisions, scan `{sharedDir}/{botId}/spans/spans-<today>.jsonl` for the most recent turn span per session_id.
2. Rehydrate per-session state from the latest span: `currentTier` ← `tier_used`, `consentSource` ← `attributes.consent_source`, `turnsAtCurrentTier` ← derived by counting consecutive same-tier turns backwards, `userRequestedTier` ← if the span had `tier_chosen_by ∈ {"user_request", "ui_chip"}`, set to last user-chosen tier.
3. `recentStruggleAverage` is acceptable to lose — rebuilds within 3 turns.
4. If no prior span for a session, treat it as turn 1 only when the OC ctx confirms it (`turnIndex === 0` in ctx). Otherwise treat as "in-flight, no prior data" — fail-open by returning `{}` from the hook and letting `ModelRouter` use the default. Emit `cascade_cold_start_blind` Signal once per session per process.

**ModelRouter's `setUserTier` state** also persists across restarts via the same mechanism. Existing PR #1629 keeps this in-memory; cascade extends to rehydrate from spans on startup.

#### Telemetry write failures

**Problem:** `CascadeTelemetry.recordTurnSpan` is "best-effort" but the blast radius is currently undefined. Disk full, ACL wrong, file locked.

**Required behavior:**

1. Every write wrapped in try/catch that NEVER throws into the hot path (already implemented in Phase 1).
2. K consecutive write failures (default K=10) within a single process emit a `cascade_telemetry_lost` Signal with the path and errno, then back off to 1-in-100 retry (the first write attempt of every 100th turn) to detect recovery.
3. JSONL dir creation is part of plugin startup with explicit ENOENT handling — not assumed-present.
4. `cascade_telemetry_lost` Signal has its own watchdog: if firing for >24h, emit `cascade_blind` higher-severity Signal — the audit layer is operating without data.

#### Config-load failures

**Problem:** Malformed `network.json::cascade` block. JSON parse error, unknown field, string-where-number, missing block entirely.

**Required behavior:**

1. Schema validate at startup. Unknown/extra fields → use defaults, log INFO once.
2. Type mismatch on a known field (e.g., `tier3_escalate_threshold: "foo"`) → use default for that field, emit `cascade_config_field_invalid` Signal naming the field.
3. JSON parse failure on `network.json` → cascade disabled pod-wide, emit `cascade_config_invalid` Signal. Do NOT silently use defaults — operator must see this. ModelRouter falls back to pre-cascade behavior (the existing keyword classifier during Phase 1-3 migration; the post-cutover default in Phase 3+).
4. Per-bot `tiers.json::cascade` malformed → per-bot config ignored, pod default used, `cascade_bot_config_invalid` Signal fired naming the bot.

#### OC payload contract drift

**Problem:** `agent_end.messages` shape changes in a new OC version (renamed, nullable, different content block semantics). StruggleDetector silently produces wrong results (typically score=0 because regex finds nothing in an unexpected shape).

**Required behavior:**

1. StruggleDetector contract check: if `event.messages` is undefined, not an array, OR empty when `event.success === false`, emit `cascade_payload_unexpected` Signal with the OC version (from existing version probe) and observed payload keys.
2. Return a sentinel `StruggleSignal` with `score: null` (NOT 0) so downstream consumers can distinguish "didn't struggle" from "couldn't measure."
3. Audit layer (§ 2.3) ignores null-score turns. Tile metrics surface "% measurable" so operators see when the contract is degrading.

#### Hook double-fire / out-of-order

**Problem:** OC fires `agent_end` for turn N-1 after `before_model_resolve` of turn N has already fired (documented out-of-order behavior). Or `agent_end` fires twice (retry path). Result: stale struggle signal, double-counted persistence, ask-hint cooldown decremented twice.

**Required behavior:**

1. StruggleDetector keyed by `(session_id, turn_index)`. Second fire of the same key is a no-op with a debug log.
2. CascadeController reads `lastStruggleSignal` by turn_index (not by recency). If the signal for turn N-1 hasn't arrived by `before_model_resolve` of turn N, fall through to current tier and emit `cascade_signal_lag` if this happens >5% of turns over a sliding window.

#### tier_used vs tier_intended divergence

**Problem:** Plugin returns tier X from `before_model_resolve`; OC may not honor it (per multiple memories around model-override loss). Without explicit tracking, cascade's calibration loop is built on intent, not truth.

**Required behavior** (Phase 1 implementation now matches this):

1. `OpikSpan.tier_used` is set from the **actual model billed** via `ModelRouter.getTierForModel(llm.model)`. Source of truth.
2. `OpikSpan.tier_intended` is set from the controller's verdict. Source of intent.
3. When `tier_used != tier_intended`, set `tier_divergent: true` (Phase 1 implementation does this) AND emit `cascade_override_violated` Signal when divergence persists for ≥3 consecutive turns in a session.
4. Audit layer uses `tier_used` (truth) for misclassification metrics. Cascade controller's hysteresis state ALSO uses `tier_used` once available — falling back to `tier_intended` only on the first turn after a span.

#### Session key uniqueness and concurrent sessions

**Problem:** `sessionKey` collision across concurrent sessions of the same bot leaks state between sessions.

**Required behavior:**

1. `sessionKey = (bot_id, session_id)` everywhere — never just `session_id`.
2. ModelRouter, CascadeController, and `setUserTier` state are all scoped by this composite key.
3. Phase 2 must include a concurrent-sessions test fixture that confirms `setUserTier` and cascade state on one session don't bleed to a parallel one.

#### Span retention and disk pressure

**Problem:** Cascade is the first hot-path span emitter (every turn, every bot). At pod-scale, JSONL volume could fill the shared dir. Existing `signals/` and `arbiter/` data lives there too — disk fill cascades into Signal-store write failures.

**Required behavior:**

1. Hot-path JSONL spans rotate daily (existing behavior) AND retain max 14 days. `signals/retention.py` extends to cover `{sharedDir}/{botId}/spans/` and `{sharedDir}/observability/spans/`.
2. Size-based fallback: prune oldest day-files if total cascade-span volume on disk exceeds 1GB.
3. A new `disk_pressure_observability` watchdog (lightweight Python daemon) fires `disk_pressure_observability` Signal when the spans dir alone exceeds 70% of a configurable limit.
4. Documented per-day volume estimate in spec § 9 (bytes-per-span × turns-per-bot-per-day × bot count) so operators can size disk accordingly.

#### MCP tool input validation

**Problem:** Bot calls `session.set_tier` with garbage — unknown choice, missing reason, wrong session context.

**Required behavior:**

1. Unknown `choice` → tool returns structured error with valid options enumerated. Bot sees error in next turn, can correct or escalate via cascade itself.
2. Case-insensitive: `"Power"`, `"POWER"`, `"power"` all valid.
3. Missing `reason` → accept but log at INFO.
4. Tool invoked outside an active session context → return error; cascade controller is not affected.
5. ALL tool error paths preserve cascade controller state — a broken tool call never corrupts cascade.

#### Ask-hint / set_tier race classification

**Problem:** Ask-hint state is per-session; cross-session interleaving could misclassify `consent_source`.

**Required behavior:**

1. Ask-hint state scoped to `(bot_id, session_id)` — never bot-wide.
2. `set_tier` called from a different session does NOT qualify as `ask_hint_agreed` even if there was a recent ask-hint in some other session.
3. The "active ask-hint window" is measured in turn count of *that* session, not wall-clock or aggregate.
4. When cascade state is rehydrated from spans (per cold-start above), the latest span's `tier1_ask_emitted` + `tier1_ask_cooldown_remaining` fields drive the window check.

#### Failure-mode signal taxonomy

For operator clarity, every new Signal type introduced in this section is enumerated:

- `cascade_cold_start_blind` (info) — cascade lost in-memory state, fell back to fail-open
- `cascade_telemetry_lost` (warning) — K consecutive span-write failures
- `cascade_blind` (critical) — telemetry lost for >24h
- `cascade_config_invalid` (critical) — pod-wide config broken; cascade disabled
- `cascade_config_field_invalid` (warning) — one field bad; using default for it
- `cascade_bot_config_invalid` (warning) — one bot's tiers.json broken
- `cascade_payload_unexpected` (warning) — OC hook payload contract drift
- `cascade_signal_lag` (warning) — struggle signal arriving after the turn it'd affect
- `cascade_override_violated` (warning) — tier_used ≠ tier_intended for ≥3 consecutive turns
- `disk_pressure_observability` (warning) — spans dir near capacity
- Plus the density Signals from § 2.6 (`session_tier1_cap`, `pod_tier1_concurrency_cap`, etc.)

All Signals route through the existing signal store. Operators see them on the existing Alerts page. None are silently ignored.

---

## 3. Telemetry — extending Opik

### 3.1 What's already shipped

Per the 2026-05-26 audit:

- `packages/analyzer/observability/opik_client.py` — Apache-2.0 wrapper, three backends (`OpikBackend`, `JsonlBackend`, `DisabledBackend`), lazy SDK load
- JSONL fallback at `/Users/Shared/evolve/observability/spans/` — always populated
- Existing span emitters: `embedding_monitor.py`, `evolve_watchdog/events.py`, `reporter.py`
- Installation: `evolve-admin install-infra-jobs --with-opik`
- 43 tests covering backend factory, span round-trip, signal adapter, cost path

### 3.2 What this spec adds

**New emitter:** `packages/plugin/src/observer/CascadeTelemetry.ts` — emits one span per turn from the OC plugin hot path. **This is the first hot-path emitter** (everything shipped so far is error/audit class).

**Extended `OpikSpan` dataclass** at `packages/analyzer/observability/opik_client.py:OpikSpan`:

```python
@dataclass
class OpikSpan:
    # ... existing fields ...
    span_kind: Literal["error", "watchdog", "cost", "turn"]  # extended

    # Cascade fields (populated only when span_kind == "turn")
    session_id: Optional[str] = None
    turn_index: Optional[int] = None
    bot_id: Optional[str] = None
    trigger_kind: Optional[str] = None     # "user_turn" | "heartbeat" | "cron_app" | "subagent" | ...
    tier_used: Optional[str] = None        # "tier0" | "tier1" | "tier2" | "tier3"
    tier_chosen_by: Optional[str] = None   # "cascade" | "user_request" | "spend_cap" | "default" | "classifier"
    struggle_score: Optional[float] = None
    struggle_features: Optional[Dict[str, float]] = None
    triviality_score: Optional[float] = None  # Phase 2+
    triviality_features: Optional[Dict[str, float]] = None  # Phase 2+
    escalation_event: Optional[Literal["escalated", "deescalated", "held"]] = None
    deescalation_reason: Optional[Literal["triviality", "hysteresis_stable"]] = None
    user_request_tier: Optional[str] = None  # if set_tier was called this session
    user_request_reason: Optional[str] = None
    consent_source: Optional[Literal["ui_chip", "ask_hint_agreed", "bot_initiated"]] = None
    turns_at_current_tier: Optional[int] = None  # tracks hysteresis state
    # Tier1 consent flow (§ 2.4 + § 2.2). Phase 2+.
    tier1_ask_emitted: Optional[bool] = None       # True if cascade injected an ask-hint this turn
    tier1_ask_outcome: Optional[Literal["agreed", "declined", "no_response", "pending"]] = None
    tier1_ask_cooldown_remaining: Optional[int] = None  # turns until cooldown lifts
```

**Why span-per-turn (not span-per-session):** Per-turn is the unit of cascade decision. Sessions are derived by grouping turns by `session_id`. Opik supports trace→span hierarchy natively for this.

### 3.3 Session-level rollup

Add `packages/analyzer/observability/session_rollup.py`:

- Reads turn spans, groups by session_id
- Produces SessionSummary: max tier used, escalation count, total struggle peaks, cost
- Feeds the Audit Layer (§ 2.3) and the per-bot tile metrics

---

## 4. Configuration model

Three layers, in precedence order from most-specific to least:

### 4.1 Per-session: user request via MCP tool

**Reuses existing primitive.** The admin UI already has a tier-override chip (`Auto/Fast/Standard/Power` per PR #1629, [spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md)), which calls `ModelRouter.setUserTier(sessionKey, choice)`. Cascade does not invent a parallel mechanism — it exposes the same primitive to messaging-app surfaces (Telegram, Slack DMs, etc.) where there's no UI chip.

**Tool exposed to every cascade-enabled bot:** `session.set_tier`

```typescript
// MCP tool schema
{
  name: "session.set_tier",
  description: "Set the model capability tier for the rest of this session. " +
               "Call when the user explicitly asks for more or less reasoning " +
               "capability (e.g., 'think harder about this', 'just give me a " +
               "quick answer', 'this is critical'), OR when responding to a " +
               "cascade ask-hint and the user has consented.",
  input_schema: {
    choice: { enum: ["fast", "standard", "power", "auto"], description: "fast=tier3/Haiku, standard=tier2/Sonnet, power=tier1/Opus, auto=let cascade decide" },
    reason: { type: "string", description: "Brief paraphrase of the user's intent" }
    // consent_source NOT exposed to the bot — set by the tool handler
    // based on context (presence of an active ask-hint vs. fresh request).
  }
}
```

**Implementation:** the MCP tool handler is a thin wrapper that calls `ModelRouter.setUserTier(sessionKey, choice, consentSource)`. Vocabulary matches the UI chip exactly. `"auto"` clears any prior choice and hands control back to the cascade controller. The existing precedence stack (user choice beats cascade; spend-cap beats both) applies unchanged.

**Consent source inference (set by the handler, not the bot):**

The `consent_source` parameter on `setUserTier` is determined by the cascade controller's session state at the moment the bot calls the tool:

- `"ui_chip"` — the call originated from the admin UI chip path (`api_home_chat` per [spec-user-tier-control-2026-05-26.md](spec-user-tier-control-2026-05-26.md)). Sticky for the session — no autonomous de-escalation, no demotion.
- `"ask_hint_agreed"` — the cascade controller had an active `tier1` ask-hint in the previous N=`tier1_ask_no_response_turns` turns, and the bot called `setUserTier("power")` within that window. Allows autonomous de-escalation back to tier2 once struggle stabilizes (§ 2.2 `chooseTierUserFacing`).
- `"bot_initiated"` — the bot called `setUserTier` without an active ask-hint (e.g., the user said "just be quick" mid-conversation and the bot interpreted that as "go to fast"). Treated as sticky for the session, same as `ui_chip` — the user gave the bot a direct instruction; don't autonomously reverse it.

The bot never sees or sets `consent_source` — it's purely a server-side classification of the consent's origin. This avoids the failure mode where a confused bot claims a non-existent ask-hint to bypass stickiness.

#### Bot-initiated tier1 is rate-limited and gated

The cost stress-test review (F9) flagged that `bot_initiated` consent lets a bot grant itself tier1 on any plausible-sounding user message ("be thorough"), bypassing the ask-hint gate entirely. This is a real risk — a confused or adversarial-prompt-injected bot could escalate to Opus unilaterally.

**Server-side gates on bot-initiated tier1 escalation:**

1. **Demotion only is free.** `bot_initiated` calls that move *down* the tier ladder (`fast`, `standard`) are accepted without scrutiny. Cost only goes down.
2. **`power` requires evidence.** A `bot_initiated` call with `choice = "power"` is only honored when the tool handler can find one of:
   - An ask-hint that was injected within the last 5 turns of this session (regardless of cooldown state — even a stale hint is evidence the cascade controller thought escalation was warranted), OR
   - The user's last message contains a strong tier-power signal (length > 50 chars AND matches a conservative regex like `\b(use|with|via)?\s*(your|the)?\s*(best|smartest|most capable|deepest|strongest)\b`) — Plex-test phrases an everyday user would actually use.
3. **Without either:** the tool handler downgrades the request to `standard` (tier2), emits `bot_initiated_tier1_denied` Signal with the user message excerpt and the bot's claimed reason. The bot sees a tool result indicating the downgrade. Cascade controller is not affected.
4. **`bot_initiated` count cap per session:** at most 2 `bot_initiated` calls per session (regardless of choice). 3rd+ calls return a tool error. Prevents a confused bot from spam-calling the tool.

The signal `bot_initiated_tier1_denied` is itself an audit input — if it fires frequently on a particular bot, that bot's prompt addendum may be teaching the wrong escalation behavior, or the bot's persona may be over-eager about model capability discussion. Phase 4 audit layer flags this for review.

**Bot system prompt addendum (auto-injected by Evolve plugin for cascade-enabled bots):**

```
TIER SELECTION
You can adjust the model tier for the rest of the session via session.set_tier.
Call it when the user explicitly asks for more or less reasoning capability:
- "think harder", "this is important", "be thorough" → choice: "power"
- "just be quick", "don't overthink this", "simple answer" → choice: "fast"
- "back to normal" or after the request is handled → choice: "auto"
Don't surface "tier" or model names to the user. Translate: "Sure, I'll think
this through more carefully." The default cascade handles automatic escalation
on struggle, so only call this for explicit user requests.
```

**Bounded by per-bot ceiling.** A bot configured `max_tier: "tier2"` rejects `"power"` requests with a graceful fallback to standard, same shape as the admin-UI tier1 daily-cap handling. Logged to Opik with reason.

### 4.2 Per-bot: `tiers.json` override

**File:** `{shared_dir}/{bot_id}/tiers.json` (existing — extended)

Per-bot config mirrors the pod-wide `network.json::models.cascade` block from § 4.3, with all fields optional. Anything omitted falls through to the pod default. The schema:

```json
{
  "cascade": {
    "enabled": true,
    "user_facing": {
      "demote_threshold": 0.7,
      "tier3_repromote_threshold": 0.4,
      "tier1_ask_enabled": true,
      "tier2_struggle_persistence": 2,
      "tier2_struggle_threshold": 0.65,
      "tier1_ask_cooldown_turns": 10,
      "tier1_ask_no_response_turns": 3,
      "struggle_weights": { /* StruggleDetector weights override */ },
      "triviality_weights": { /* TrivialityDetector weights override */ }
    },
    "background": {
      "tier3_escalate_threshold": 0.6,
      "tier2_escalate_threshold": 0.75,
      "persistent_struggle_threshold": 0.7,
      "tier1_enabled": false,
      "force_default_tier": null,
      "struggle_weights": { /* StruggleDetector weights override */ }
    },
    "rationale": "Optional human-readable note about why this bot has the config it does."
  }
}
```

**Hard rules per § 2.4 — cannot be overridden per bot:**
- `user_facing.default_tier` is always tier2 (system invariant)
- `background.default_tier` is always tier3 (system invariant)
- Background user-set tier choices are not honored (no chip exists for backgrounds)

**Allowed per-bot overrides for direction-adjacent behavior:**
- `background.force_default_tier: "tier2"` — single escape hatch for user-visible cron output (morning briefing pattern). Promotes a specific bot's background sessions from tier3 to tier2 default. Does NOT flip the direction (still escalates further on struggle, doesn't demote).
- `background.tier1_enabled: true` — opt this bot's backgrounds into autonomous tier3 → tier2 → tier1 escalation. Operator's opt-in IS the consent.
- `user_facing.tier1_ask_enabled: false` — suppress the tier1 ask hint for bots whose persona shouldn't break the fourth wall.

**No per-bot-class templating up front.** We can't classify bots at creation time by archetype — what a bot "is" emerges from how the user actually uses it. All bots start with the pod default cascade config. Refinements come from two sources:

1. **Evo wizard during onboarding (see § 4.4).** The wizard collects user intent ("what's this bot for? do you want it to feel fast or thorough? do you care more about cost or capability?") and writes an initial cascade override into the bot's `tiers.json`.
2. **Audit Layer learning over time (Phase 4, § 2.3).** Sustained patterns in observed struggle/escalation produce Proposals to adjust the bot's config. Subject to normal arbiter approval — Evolve never silently retunes a bot's tier policy.

The schema *supports* per-bot overrides for the obvious edge cases (background-only bots that should pin tier3, advanced users who explicitly want everything on tier1), but those are operator-driven decisions, not Evolve guesses.

### 4.3 Pod-wide: `network.json` defaults

**Per-source config — direction is a system rule (§ 2.4), thresholds are tunable.**

**File:** `network.json` (existing — extended)

```json
{
  "models": {
    "tiers": { /* existing */ },
    "routing": { /* existing — kept for back-compat during phasing */ },
    "cascade": {
      "enabled": true,

      "user_facing": {
        "default_tier": "tier2",
        "demote_threshold": 0.7,
        "tier3_repromote_threshold": 0.4,
        "tier1_ask_enabled": true,
        "tier2_struggle_persistence": 2,
        "tier2_struggle_threshold": 0.65,
        "tier1_ask_cooldown_turns": 10,
        "tier1_ask_no_response_turns": 3,
        "tier1_destabilize_threshold": 0.2,
        "tier1_destabilize_turns": 5,
        "struggle_weights": { /* StruggleDetector weights override */ },
        "triviality_weights": { /* TrivialityDetector weights override */ }
      },

      "background": {
        "default_tier": "tier3",
        "tier3_escalate_threshold": 0.6,
        "tier2_escalate_threshold": 0.75,
        "persistent_struggle_threshold": 0.7,
        "tier1_enabled": false,
        "tier2_destabilize_threshold": 0.2,
        "tier2_destabilize_turns": 5,
        "tier1_destabilize_threshold": 0.15,
        "tier1_destabilize_turns": 5,
        "struggle_weights": { /* StruggleDetector weights override */ }
      },

      "audit": {
        "enabled": true,
        "sample_size_per_day": 20,
        "judge_model": "anthropic/claude-haiku-4-5",
        "sanity_check_rate": 0.05,
        "auto_tune_enabled": false
      }
    }
  }
}
```

**Hard rule (not in the JSON schema, enforced in code):** `user_facing.default_tier` MUST be tier2 and `background.default_tier` MUST be tier3. Per § 2.4, the cascade direction is a system invariant. If the JSON tries to flip them, the loader logs a warning and uses the invariant. This is in the JSON for explicitness, not for tuning.

**User-facing tier1 ask knobs:**
- `tier1_ask_enabled` (default `true`) — master switch. Set `false` on bots whose persona doesn't allow tier-talk (a "calm assistant" character that shouldn't break the fourth wall with "let me bring in the smart model"). When false, cascade silently continues on tier2 even when sustained struggle would otherwise warrant asking.
- `tier2_struggle_persistence` (default `2`) — how many *consecutive* turns above `tier2_struggle_threshold` before injecting the ask-hint. Single bad turn ≠ ask; sustained struggle = ask.
- `tier2_struggle_threshold` (default `0.65`) — score threshold for "this turn struggled enough to count toward persistence."
- `tier1_ask_cooldown_turns` (default `10`) — after an ask is injected and the user does NOT agree (no `session.set_tier("power")` call within `tier1_ask_no_response_turns` turns), suppress further ask-hints for this many turns. Re-arms on a fresh struggle re-firing after stable turns.
- `tier1_ask_no_response_turns` (default `3`) — grace window for the user to decide. After this many turns without a `set_tier("power")` call following an ask-hint, the cascade treats it as "user declined" and engages the cooldown.

**De-escalation knobs (Phase 3):**

User-facing tier1 → tier2:
- `tier1_destabilize_threshold` (default `0.2`) — struggle score must be below this on each of the recent turns at tier1 to qualify for de-escalation. Much lower than `tier2_struggle_threshold` (0.65) → that's the hysteresis preventing oscillation.
- `tier1_destabilize_turns` (default `5`) — required number of consecutive low-struggle turns. Conservative — short sessions never trigger this; only sustained calm at tier1.

Background tier2 → tier3 and tier1 → tier2 use the same shape with separate per-step thresholds (`background.tier2_destabilize_threshold` / `_turns`, `background.tier1_destabilize_threshold` / `_turns`).

**Defaults if unspecified:** cascade enabled, directions as the invariant requires, thresholds as shown above, tier1 disabled for backgrounds (operator opts in per bot), audit advisory-only (no auto-tune until Phase 4 ships).

**Per-bot overrides** (§ 4.2 `tiers.json::cascade`) can adjust thresholds and weights but cannot flip directions or change defaults. One exception escape hatch: `background.force_default_tier: tier2` is allowed for bots whose backgrounds are user-visible (morning briefing pattern, § 2.4). This is the only per-bot direction override; it does not extend to user-facing sessions.

### 4.4 Evo wizard integration

The evo bot-creation/bot-customization wizard (see [spec-conversational-bot-creation-wizard](project_conversational_bot_creation_wizard) in memory and `spec-evo-wizard-2026-05-05.md`) gains a short cascade-preference dialogue, integrated into the existing onboarding flow rather than as a separate step.

**What the wizard collects (in user-facing language — never says "tier"):**

1. **Use-case shape.** Open question: "what's this bot mostly going to do for you?" Free-form. Used to set initial defaults and to populate the bot's profile.
2. **Cost-vs-capability lean.** Two-option choice: "I want it to feel snappy and stay cheap" vs. "I want it to think hard when it needs to." The first biases the cascade toward staying in tier3 longer (raise `tier3_escalate_threshold`); the second loosens escalation (lower the threshold). No middle option — forcing the choice produces a real signal.
3. **Background-only check.** "Will a person ever read this bot's output directly, or is it just doing housekeeping?" If background-only, pin `max_tier: "tier3"` and disable escalation. This is the audit/heartbeat-bot path without us having to identify it by archetype.

**Wizard output:** writes `cascade` block into the bot's `{shared_dir}/{bot_id}/tiers.json`. Plus a `wizard_collected` provenance field on each setting so the Audit Layer knows what came from user choice vs. evolved defaults.

**What the wizard does NOT do:**

- Doesn't ask the user to pick a tier or a model. Tier is an implementation detail.
- Doesn't ask the user to set thresholds. The two-option lean is the only knob exposed.
- Doesn't require completion — a user who skips the wizard gets the pod defaults, which are fine.

**Mid-life adjustments.** A user can revisit the same dialogue later via evo: "evo, this bot feels too slow / too expensive / too dumb." Evo reads the bot's recent telemetry, names the trade-off in plain language, and offers the same lean choice. Settings update the same `tiers.json` block, with the new `wizard_collected` provenance.

---

## 5. What gets deprecated

The 2026-05-26 deprecation audit surfaced that **routing logic is duplicated in three places** — a design smell this spec fixes en route. The cascade controller becomes the *single* decision point; the analyzer and admin UI consume its emitted spans rather than re-deriving routing decisions independently.

### 5.1 Plugin (TypeScript) — the primary classifier

| Component | Disposition | File:line |
|---|---|---|
| `TierClassifier.ts` (keyword classifier) | Deleted Phase 3. Kept one cycle for rollback. | `packages/plugin/src/observer/TierClassifier.ts` |
| `LLMTierClassifier.ts` (Haiku fallback) | Deleted Phase 3. | `packages/plugin/src/observer/LLMTierClassifier.ts` |
| `ModelRouter.setSessionType()` + `sessionTypes` map | Deleted Phase 3. Cascade uses struggle signals, not category labels. | `ModelRouter.ts:125, 192-193, 273-280, 327-331` |
| `TurnObserver.ts` classifier calls | Replaced with struggle-detector wiring. | `TurnObserver.ts:36, 40, 42, 1221-1260, 1283, 1303` |
| `SessionSummarizer.dominantClass()` | Replaced with `dominantTier()` aggregating actual tier used per turn. | `SessionSummarizer.ts:161-170, 165, 268` |
| `ModelRouter` (rest) | **Kept.** Still owns user `/model` override, spend-cap enforcement, auth-profile routing. | — |

### 5.2 Analyzer (Python) — the parallel routing implementation

This is the surprise from the audit: there's a **second routing decision path** in Python that duplicates the plugin's logic.

| Component | Disposition | File:line |
|---|---|---|
| `select_model_for_session()` | **Delete.** Duplicates plugin routing in Python; was only correct when category labels existed. | `packages/analyzer/models.py:356-404` |
| `explain_model_selection()` | Delete or rewrite to explain cascade decisions from Opik spans. | `packages/analyzer/models.py:407-430` |
| `high_tier_ratio_for_maintenance()` | Replace with `tier_distribution_by_bot()` — bucket cost by *actual tier used*, not by inferred category. | `packages/analyzer/metrics/resolvers/cost_metrics.py:213-244` |
| `measure.py` session-class aggregation | Replace with tier-distribution aggregation from spans. | `packages/analyzer/measure.py:268-275, 302-305, 335-344` |
| `session_monitor.py` session_class reads | Replace with tier-used reads. | `packages/analyzer/session_monitor.py:30, 63, 70, 95, 177, 296` |

### 5.3 Admin UI (Python) — the third routing implementation

| Component | Disposition | File:line |
|---|---|---|
| Hardcoded routing table by session_class | **Delete.** Pure-display table that lies once cascade is live. | `evolve_admin/web/server.py:7296-7298` |
| `/api/models/tier-calibration` endpoint | Delete. Replace with `/api/models/cascade-state` exposing live cascade config + recent audit metrics. | `server.py:6922-7011` |
| Tile-metrics `t_prod`/`t_maint` columns | Replace with `t1`/`t2`/`t3` tier-distribution columns. | `server.py:4522-4529` |
| Cost-alert branching on session_class | Replace with branching on tier-used or struggle-score. | `server.py:6478` |

### 5.4 Config and persisted data

| Component | Disposition |
|---|---|
| `{sharedDir}/calibration/classifier.json` | Delete after Phase 3. Replaced by `network.json::models.cascade`. |
| `classifierHints` in `network.json` | Marked deprecated Phase 3, removed one release later. |
| `session_class` field on turn annotations / spans | **Keep writing during Phase 3** (read-compat for analyzer rollups); deprecate one release after; remove from emission entirely Phase 4. |

### 5.5 Tests

| File | Disposition |
|---|---|
| `packages/admin/tests/test_better_engine_tier0-4.py` | Audit + rewrite. May contain assertions tied to categorical labels. |
| `packages/admin/tests/test_intake_classifier.py` | Likely delete (classifier-specific). |
| `packages/analyzer/tests/test_rsi_tier_adjustment_applier.py` | Audit — tier-adjustment may still be relevant, just over different data. |

### 5.6 The "three implementations" design rule going forward

After cascade lands, **routing decisions live in exactly one place**: the cascade controller in the plugin. The analyzer and admin UI consume `OpikSpan.tier_used` (the *actual* tier the model ran at) and never recompute routing decisions. Any future change to routing policy touches exactly one file. The audit caught this regression late — Phase 3 explicitly fixes it.

---

## 6. OC hook audit findings (2026-05-26)

Audit ran 2026-05-26. **Result: partial — workaround required.** Findings below; the struggle detector (§ 2.1) is designed around this reality.

### 6.1 What hooks Evolve currently uses

Eight hooks registered in `TurnObserver.ts`:

| Hook | Line | Fires | Payload | Useful for cascade? |
|---|---|---|---|---|
| `session_start` | 369 | session begins | — | — |
| `llm_output` | 397 | after LLM call | `{sessionId, model, provider, usage}` | partial (usage but no tool data) |
| `agent_end` | 432 | turn ends | `{messages[], success, durationMs}` | **timing only** — no tool outcomes |
| `session_end` | 452 | session ends | unspecified | — |
| `before_agent_reply` | 481 | post-LLM, pre-reply | unspecified | — |
| `before_prompt_build` | 528 | pre-prompt | unspecified | — |
| `before_model_resolve` | 607 | pre-model-pick | `{prompt, attachments?, ctx:{sessionKey, sessionId, ...}}` | **yes — cascade controller hooks this** |
| `before_agent_run` | 719 | pre-agent | varies | — |

### 6.2 The gap

- **No tool-call-level hooks exist.** No `before_tool_call`, `after_tool_call`, `tool_result`, or `on_tool_error`. The hook-governance spec ([docs/spec-hook-governance-2026-05-10.md:327-328](spec-hook-governance-2026-05-10.md)) explicitly states this is a non-goal for the plugin API.
- **`agent_end` does not carry tool outcomes.** Payload is `{messages, success, durationMs}` only. No tool calls made, no errors, no completion reason.
- **No `turn_end` / `after_turn` hook exists.** `agent_end` is the closest analog and it's payload-poor.

### 6.3 The workaround — simpler than the audit suggested

While auditing the hooks, we found something better than the original "read turns-jsonl post-hoc" plan: **`agent_end.messages` already carries Anthropic-style content blocks**, including `tool_use` and `tool_result` (with `is_error: true` on failed tool calls). So tool-call outcomes ARE in the hook payload — they're just nested inside the messages array, not surfaced as a separate field.

**Phase 1 actually shipped (verified in `StruggleDetector.ts`):**

1. Plugin hooks `agent_end` — gets `event.messages`, `event.success`, `event.durationMs`
2. Struggle detector parses message content blocks: counts `tool_use` blocks (per-tool retry detection), counts `tool_result` blocks with `is_error: true` (error count), regex over text blocks (restart markers, clarification markers)
3. No file read needed for v1 features.

**Future enrichment via turns-jsonl** remains available for features that need data NOT in the messages array — per-tool wall-clock duration, completion reason, prompt-side cache state. Cost: one filesystem read per turn, ~single-digit ms. Same pattern as `cost_event_converter.py`. Defer until a specific feature demands it.

**Latency** is now effectively zero (struggle compute is in-process from the hook payload, no I/O). Cascade controller in Phase 2 has the signal available synchronously at `agent_end` time, well before `before_model_resolve` fires on turn N+1.

### 6.4 Upstream issue we should file

Independent of shipping, file an OC issue asking for either:

- A richer `agent_end` payload including tool calls, tool results, completion reason, OR
- A new `turn_end` hook with that payload

Per [feedback_dont_reimplement_upstream](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_dont_reimplement_upstream.md), upstream is the right home for this. The file-read workaround is fine for v1 (proven pattern, low latency) but the hook is where this *should* live long-term. When/if OC ships the richer payload, struggle detector swaps the data source without changing its interface.

---

## 7. Phasing

### Phase 1 — Telemetry (target: 1 week)

- Extend `OpikSpan` with cascade fields
- Implement struggle detector — emit signals to Opik, **no routing changes**
- Implement `CascadeTelemetry.ts` hot-path emitter
- Implement session rollup
- Existing keyword classifier keeps deciding routing
- **Exit criteria:** 2 weeks of clean span data across all bots; struggle signals visibly correlate with manually-identified hard sessions

### Phase 2 — Shadow cascade (target: 2 weeks after Phase 1 exit)

Includes the **TrivialityDetector**, the **source-branching cascade controller**, and the **tier1 consent flow** (§ 2.2 + § 2.4). Bigger than originally scoped because of the asymmetric-direction design AND the consent flow.

- Implement `TrivialityDetector.ts` — pure-function sibling of StruggleDetector for user-facing demotion signal
- Implement `CascadeController.ts` in shadow mode — computes verdict (branches on `trigger_kind`), doesn't apply
- Implement tier1 ask-hint mechanism — cascade emits `AskHint` when sustained struggle warrants it; injection wired into `before_prompt_build` via `appendSystemContext` (same plumbing as existing stay-quiet directives). Phase 2 logs the ask-hints but doesn't yet inject them in shadow mode — we want to validate when cascade *would have* asked before we actually start asking.
- Read `trigger_kind` from agent_end ctx; default to `user_turn` if missing (safer than guessing background)
- Emit shadow-verdict-vs-keyword-verdict comparison spans, plus ask-hint emission spans
- Phase 2 admin UI page: "Shadow cascade disagreements" — what cascade would have done differently, stratified by source. Plus an "ask-hints emitted" table — when cascade *would have* asked the user to escalate to tier1.
- **Exit criteria:** disagreements between shadow cascade and current classifier are *explainable* **separately for user_turn and background sessions**. Each class of disagreement either (a) clearly favors cascade, (b) clearly favors current, or (c) is a real ambiguity we can decide policy on. No remaining "I don't understand why these disagreed" cases for either source. **Ask-hint emission rate is sane** — under 5% of user-facing sessions should fire one in shadow mode (rare events; if firing on 20% of sessions, the threshold is wrong). The cron-but-user-visible bots (morning briefing class) have been identified and either configured with `force_default_tier: tier2` or confirmed-fine on Haiku.

### Phase 3 — Live cascade + de-escalation + deprecation consolidation (target: 3 weeks after Phase 2 exit)

This phase is **longer than originally scoped** because (1) the deprecation audit (§ 5) surfaced three parallel routing implementations that all need to migrate, and (2) we're adding de-escalation paths (§ 2.2 + § 2.4) which need their own shadow-then-live progression. Order matters:

1. **Plugin cutover (escalation only).** Flip `before_model_resolve` to cascade controller — but only the escalation gates. De-escalation gates land disabled (return current tier). Cascade emits `tier_used` on every span. `session_class` still written for read-compat. Rollback knob: `network.json::models.cascade.enabled: false`.
2. **Implement `session.set_tier` MCP tool**, wire into evo + the auto-injected system-prompt addendum. Implement `consent_source` inference in the tool handler (§ 4.1).
3. **Analyzer migration.** Rewrite `select_model_for_session` and `high_tier_ratio_for_maintenance` to consume `OpikSpan.tier_used`. Replace `measure.py` aggregations.
4. **Admin UI migration.** Replace hardcoded routing table, tile-metric columns, cost-alert branching. Add new `/api/models/cascade-state` endpoint.
5. **After 1 stable week post-step-4**: delete `TierClassifier.ts`, `LLMTierClassifier.ts`, `setSessionType`, analyzer `select_model_for_session`, admin-UI routing table, calibration.json plumbing. Drop `session_class` writes.
6. **De-escalation enablement.** With one week of clean data from step 1-5, flip de-escalation gates on (background tier2→tier3, background tier1→tier2, user-facing tier1→tier2 conditional on `consent_source === "ask_hint_agreed"`). Initially with conservative thresholds (longer destabilize_turns, lower destabilize_threshold). Tune thresholds against the observed struggle distribution from the live escalation data.

**Exit criteria:** cost trend flat-or-down on cascade-enabled bots vs. pre-cascade baseline. No struggle-induced UX regressions reported. No Signals firing about cascade misbehavior. **All three routing implementations consolidated into one (the cascade controller).** Tile metrics + cost rollups still produce sensible numbers post-migration. **De-escalation events emit at expected rates** (not zero — means thresholds too tight — and not on >25% of escalated sessions — means thrash risk).

### Phase 4 — Audit loop (target: after Phase 3 stable for 2 weeks)

- Implement daily Haiku-judge audit job
- Initially **advisory only** — writes report, emits Signals on metric deviation, doesn't tune
- Add admin UI page showing audit metrics over time
- After 4 weeks of advisory operation: enable `auto_tune_enabled` for threshold adjustments via Proposal flow (subject to normal arbiter approval)
- **Exit criteria:** audit metrics show stable convergence; auto-tuned thresholds outperform initial-guess thresholds on misclassification metrics

### What we explicitly aren't doing

- Training a custom classifier model (no labeled corpus exists; cascade dissolves the need)
- Adopting RouteLLM weights (single-turn Chatbot Arena data doesn't transfer; revisit after Phase 4 data shows whether a predictive prior would actually help)
- Building feedback UI for end users (won't get used; messaging-app surface lacks the affordance; per [feedback_pod-admin_workflow_design_ship_retrospect](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_pod-admin_workflow_design_ship_retrospect.md), the real feedback signal is downstream use, not explicit ratings)
- Preserving productive/maintenance categorical labels (the abstraction is wrong; let it go)

---

## 8. Risk + mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| Struggle detector fires false positives → over-escalation → cost regression | Medium | Shadow phase catches before live; per-bot ceiling caps damage; spend-cap fallback still works |
| Struggle detector under-fires → user-facing UX regression on hard sessions stuck at tier3 | Medium | Shadow phase catches; Audit Layer flags missed escalations once Phase 4 lands |
| Triviality detector demotes too aggressively → mid-session quality drop on real questions | Medium | Demotion requires positive evidence (high triviality AND low struggle); re-promote path on subsequent struggle; shadow phase catches over-demotion before live |
| Tier1 ask-hint fires too often → bot pesters user about escalation | Medium | `tier2_struggle_persistence` requires N consecutive turns (not single bad turn); `tier1_ask_cooldown_turns` suppresses re-asks; per-bot `tier1_ask_enabled: false` for bots whose persona shouldn't break the fourth wall; Phase 2 shadow phase tunes the threshold against observed rates |
| Bot parrots the ask-hint system text back to the user | Low | Hint phrased as off-the-record system signal with delimiter the model is trained to ignore (same plumbing as existing stay-quiet directives); Phase 2 shadow logs include the model's response for review before live injection |
| User says "yes" to ask but bot doesn't call `session.set_tier` | Low-Medium | Bot prompt addendum (§ 4.1) is explicit about the tool name; Phase 4 audit layer flags user-agreement-without-set-tier cases as a class of misclassification |
| Thrash between adjacent tiers (escalate → de-escalate → re-escalate ...) on sessions with intermittent struggle | Medium | Hysteresis: destabilize thresholds (~0.2) are much lower than escalation thresholds (~0.6), creating a stable buffer zone. Phase 3 conservative initial thresholds; observe before tightening. |
| Silent de-escalation surprises a user who explicitly wanted tier1 (via UI chip) | Medium-Low | `consent_source` distinction: `ui_chip` is sticky-no-auto-deescalate; only `ask_hint_agreed` qualifies for autonomous tier1 → tier2 drop. Per-session telemetry records the source so we can audit any case where stickiness was wrongly broken. |
| Bot tries to spoof `consent_source` to bypass stickiness | Low | The bot never sees or sets `consent_source` — it's classified server-side by the tool handler based on whether an ask-hint was active in the recent N turns. No bot-controlled field. |
| `trigger_kind` misclassified — a cron-triggered session that's user-visible (morning briefing) defaults Haiku and quality suffers | Medium-Low | Per-bot `force_default_tier: tier2` override (§ 2.4); Phase 2 shadow review specifically calls out the user-visible-cron pattern as an exit criterion |
| Subagent inheritance gets the parent kind wrong (e.g., user-spawned subagent treated as background) | Low | Default to user-facing for subagents when parent kind is unclear (safer than guessing background); per-bot config can override |
| `agent_end` payload missing `trigger` field in some OC versions | Unknown | Default to `user_turn` when missing — safer fallback than guessing background; emit log once per process |
| Opik hot-path emission adds latency to turn loop | Low | Existing emitters use async fire-and-forget pattern; JSONL fallback is local-disk only; verify in Phase 1 |
| User `set_tier` requests get triggered spuriously (bot misreads intent) | Low-Medium | Tool description is conservative; bound by per-bot ceiling; logged with reason for audit |
| Haiku-judge drifts in Phase 4 | Medium | Sonnet sanity check on 5% sample; disagreement-rate Signal flags drift |
| Existing classifier removal breaks something we didn't anticipate | Low-Medium | Phase 3 keeps code for one cycle with rollback knob; deletion is reversible until stable |

---

## 9. Cost analysis

**Per-turn overhead:** struggle detector is pure TypeScript, single-digit-microseconds per turn. Opik span emission is async fire-and-forget. **Effectively zero hot-path cost.**

**Audit layer:** ~20 sessions/day × ~5K tokens × Haiku pricing ≈ $0.05/day. Sanity check 5% with Sonnet ≈ $0.02/day. **Total under $0.10/day across pod.**

**Expected savings — recalibrated from earlier draft:** the original 30-60% figure was structurally optimistic. FrugalGPT and Cascadia's headline savings assume starting at GPT-4 and cascading *down*. Evolve's user-facing default is already Sonnet (not Opus), so the biggest cost-saving lever in the literature doesn't apply here.

Realistic decomposition:

- **User-facing savings: 5-15%.** Only available from turn-2-onward demotion to tier3 when work is clearly trivial. Most user sessions are 1-3 turns total, so the demotion only catches 1-2 turns per qualifying session. Net savings depend on the trivial-session share of total traffic.
- **Background savings: 0-5%.** Backgrounds already default tier3 (per the post-incident fixes from 2026-05-20). The only new cost reduction comes from preventing escalation in the cross-bot-correlation case (§ 2.6 cross-bot correlation) — when a flaky upstream tool causes coordinated escalation that the watchdog suppresses.
- **New costs:** tier1 escalations (rare but ~5-10× expensive when they fire), tier3→tier2 re-promotion on user-facing sessions that were demoted too aggressively, audit layer ~$0.10/day, watchdog daemon cost (negligible).
- **Net effect: highly dependent on tier1 escalation frequency.** A pod with low ask-hint emission rate (<2% of sessions) and well-tuned thresholds should see ~10% net savings on user-facing spend. A pod hitting the failure patterns in the stress-test review (oscillation, subagent fan-out, escalation storms) could see *negative* savings until guardrails are tuned.

**Concrete target before Phase 3 cutover:** Phase 1 telemetry produces a per-pod projection. **If the projection shows <5% net savings or any net-loss scenario, hold Phase 3 and rework thresholds.** This is an explicit kill-switch gate — we do not flip cascade live just because the code is ready.

**Per-day volume estimate** (for disk-pressure planning per § 2.7): ~2KB per span × ~100 turns/bot/day × ~10 bots × 30 days = ~60MB/month. Headroom is fine on the mini's storage, but the retention contract (§ 2.7) bounds it explicitly.

---

## 10. Verification plan

Per [feedback_two_pass_review_workflow](../../.claude/projects/-Users-pod-admin-GitHub-evolve/memory/feedback_two_pass_review_workflow.md), each phase has a build-self-review + independent-reviewer pass before merge.

### Phase 1 verification

- Span emission test: spawn 10 sessions, confirm 1 span/turn lands in JSONL + Opik
- Struggle feature unit tests: each feature has a fixture turn that exercises it
- Shadow disagreement metric: report struggle score distribution vs. existing classifier verdict across 200 real sessions

### Phase 2 verification

- Shadow cascade verdicts logged for 500+ sessions
- Manual review of N=50 disagreements between cascade and current classifier — categorize each as cascade-wins / current-wins / real-policy-decision
- Cost projection: "if cascade had been live, what would this week have cost?"

### Phase 3 verification

- Cost-trend dashboard: 7-day pre-cutover baseline vs. 7-day post-cutover, per bot
- UX regression watch: explicit Signal type `cascade_underprovisioned_session` for sessions where struggle accumulated without escalation actually happening (catches threshold misconfig)
- Rollback drill: confirm `cascade.enabled: false` in `network.json` reverts cleanly without restart

### Phase 4 verification

- Audit-of-audit: Sonnet sanity sample agreement rate ≥ 85%
- Threshold proposals: at least 4 weeks of advisory-only before auto-tune, and each auto-tune proposal goes through normal arbiter approval, not silent application

---

## 11. Relationship to existing systems

- **ModelRouter.ts:** kept; demoted to "spend-cap enforcer + user-override respecter + auth-profile router." Cascade controller takes over session-type-based routing.
- **Analyzer routing logic** (`packages/analyzer/models.py:select_model_for_session`): **deleted Phase 3.** This was a parallel Python implementation of the plugin's routing decision; cascade makes it redundant. The audit (§ 5.6) called this out as a design smell — routing decisions belong in exactly one place going forward.
- **Admin UI routing table** (`evolve_admin/web/server.py:7296-7298`): deleted Phase 3. Replaced by a live view of cascade state.
- **Signal store:** cascade-related Signals (`cascade_underprovisioned`, `audit_drift`, `struggle_burst`) land here normally.
- **Arbiter / Proposals:** Phase 4 auto-tuning emits Proposals through the normal arbiter flow — no shortcut.
- **Cost watchdog:** unchanged; the `model_override_violated` detector remains the catch-all for "wrong model billed" regardless of cascade verdict.
- **Per-bot daily_cap_usd:** unchanged; cascade respects spend-cap fallback to tier3.
- **Bot-tile metrics:** replace `t_prod`/`t_maint` columns with `t1`/`t2`/`t3` tier-distribution + escalation-count, fed from `OpikSpan.tier_used` rather than `session_class`.
- **Evo wizard** (`spec-evo-wizard-2026-05-05.md`): extended with cascade-preference dialogue (§ 4.4).
- **OC plugin API:** no upstream change required for v1; file an issue requesting richer `agent_end` payload (§ 6.4) for v2.

---

## 12. Open questions

1. **Multi-modal struggle features.** Some bots have vision/file inputs. Do those introduce new struggle signatures (e.g., "model couldn't read the screenshot")? Defer to Phase 4 audit findings.
2. **Cross-session learning.** Today struggle state is per-session. Should we keep a per-bot "this bot tends to need tier2 in the mornings" prior? Probably yes eventually, but Phase 4+; doesn't affect v1 architecture.
3. **Tier0 (Judge).** Not currently part of cascade. Stays a separate routing target for cross-model evaluation. Worth confirming this is fine and not creating an integration hazard.
4. **`session.set_tier` discoverability for non-evo bots.** All bots get the tool, but does the system prompt addendum get auto-injected for *every* cascade-enabled bot, or opt-in? Recommend auto-inject with config knob to disable.
5. **Wizard re-prompt cadence.** When does evo proactively offer to re-tune a bot's cascade config? Triggered by Audit Layer signals ("this bot has been escalating 80% of sessions for two weeks — want me to bump its default?"), or only when the user complains? Probably "Audit-Layer-triggered with opt-in surfaced via evo," but worth confirming.
6. **Migration of in-flight `session_class` data.** Existing Opik spans + analyzer rollups have `session_class` values from the old classifier. Do we backfill `tier_used` from those (where known) or just start fresh at cascade-cutover? Recommend: start fresh, treat pre-cutover data as historical-only. Aggregations for tile metrics span both eras during transition via a dual-read shim.
7. **Subagent → parent kind resolution.** OC's subagent spans should carry parent_session_id. Confirm in Phase 2 audit; if not, subagents default to user-facing as the safer fallback (background subagents misclassified as user might cost a bit more but don't degrade UX; user subagents misclassified as background would visibly degrade UX).
8. **User-visible cron pattern discovery.** § 2.4 names morning briefing as the canonical case; how many other bots have user-visible cron output? Phase 2 needs a discovery step (audit cron-triggered sessions whose outputs feed into a chat / notification surface). The data is in the existing channel records — `cron-event` channel sessions that eventually emit to `telegram` or `slack` channels indicate user-visible output.

---

## 13. Appendix: prior-art references

- [RouteLLM (LMSys, Apache 2.0)](https://github.com/lm-sys/RouteLLM) — predictive routing; not adopted but useful prior art
- [FrugalGPT (arXiv:2305.05176)](https://arxiv.org/abs/2305.05176) — the cascade pattern
- [Cascadia (arXiv:2506.04203)](https://arxiv.org/abs/2506.04203) — production cascade serving + LLM-as-judge
- [Semantic Agreement (arXiv:2509.21837)](https://arxiv.org/abs/2509.21837) — training-free deferral
- [LLMs Encode Their Failures (arXiv:2602.09924)](https://arxiv.org/abs/2602.09924) — mechanistic basis for struggle prediction
- [Knowledge Distillation in Automated Annotation (arXiv:2406.17633)](https://arxiv.org/abs/2406.17633) — empirical basis for LLM-as-judge labels
- [Detecting AI Agent Failure Modes (Latitude)](https://latitude.so/blog/ai-agent-failure-detection-guide) — struggle signal taxonomy
- [Opik (Comet, Apache 2.0)](https://github.com/comet-ml/opik) — telemetry substrate
