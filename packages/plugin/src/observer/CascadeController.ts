/**
 * CascadeController — per-session decision engine (Phase 2 scaffold).
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 2.2.
 *
 * Branches on session source (spec § 2.4) and applies:
 *   - User-facing: default tier2, demote on triviality (high triviality
 *     + low struggle), escalate to tier1 via ask-hint flow on sustained
 *     struggle, de-escalate tier1→tier2 silently when consent_source
 *     was ask_hint_agreed and struggle stabilizes.
 *   - Background: default tier3, escalate to tier2 on struggle,
 *     escalate to tier1 only with per-bot tier1_enabled opt-in,
 *     de-escalate tier2→tier3 / tier1→tier2 with hysteresis when
 *     struggle stabilizes.
 *
 * **Phase 2 shadow mode:** the controller computes a verdict per turn
 * but does NOT drive routing. The keyword classifier (via
 * ModelRouter.setSessionType) still owns the actual `before_model_resolve`
 * decision. CascadeController's verdict is recorded into the cascade
 * telemetry span as `cascade.shadow_verdict.*`. Operator reviews
 * shadow-vs-actual disagreements; once explained, Phase 3 flips the
 * `cascade.enabled` flag to live.
 *
 * **Auto-bootstrap design (2026-05-27 directive):** controller ships
 * with sensible default thresholds from the spec. Phase 4 audit layer
 * tunes them later from labeled outcomes — but the defaults are not
 * guesses, they're calibrated against the literature and operator
 * intuition. Safe-on-day-one.
 *
 * State management:
 *   - Per-session in-memory state (currentTier, struggle history,
 *     ask-hint cooldown, consent_source, turnsAtCurrentTier).
 *   - Cleared on session_end via clearSession.
 *   - NOT persisted across plugin restarts (Phase 1 spec § 2.7 cold-
 *     start rehydration is a separate concern; deferred until Phase 3
 *     when state durability matters because cascade is live).
 */

import type { StruggleSignal } from "./StruggleDetector.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";

/**
 * Minimal triviality-signal shape the controller consumes. We don't
 * import TrivialityDetector directly to keep CascadeController
 * landable independently of that module — both will exist in the
 * final pod, but the type dependency would cross PR boundaries.
 * Structurally compatible with TrivialityDetector's StruggleSignal.
 */
interface TrivialitySignalShape {
  score: number | null;
  features?: Record<string, number>;
  raw?: Record<string, number>;
  payload_drift?: string | null;
}

// ── Public types ─────────────────────────────────────────────────────────────

export type Tier = "tier0" | "tier1" | "tier2" | "tier3";

export type TriggerKind =
  | "user_turn"
  | "heartbeat"
  | "cron_app"
  | "subagent"
  | "summarizer"
  | "classifier"
  | "task_extractor"
  | "fallback"
  | "unknown";

export type ConsentSource = "ui_chip" | "ask_hint_agreed" | "bot_initiated";

export interface AskHint {
  kind: "consider_tier1_escalation";
  struggle_features: Record<string, number>;
  struggle_raw: Record<string, number>;
  turns_struggling: number;
}

export interface CascadeDecision {
  /** Tier the controller would route this turn to. Shadow mode: NOT applied. */
  tier: Tier;
  /**
   * When present, the bot is encouraged to ask the user about escalating
   * to tier1. Shadow mode: hint is recorded but NOT injected into the
   * next turn's system context.
   */
  askHint?: AskHint;
  /** Whether this verdict reflects a tier change from the prior turn. */
  escalation_event?: "escalated" | "deescalated" | "held";
}

export interface CascadeControllerConfig {
  enabled: boolean;
  user_facing: UserFacingConfig;
  background: BackgroundConfig;
}

export interface UserFacingConfig {
  default_tier: Tier;
  demote_threshold: number;
  tier3_repromote_threshold: number;
  tier1_ask_enabled: boolean;
  tier2_struggle_persistence: number;
  tier2_struggle_threshold: number;
  tier1_ask_cooldown_turns: number;
  tier1_ask_no_response_turns: number;
  tier1_destabilize_threshold: number;
  tier1_destabilize_turns: number;
}

export interface BackgroundConfig {
  default_tier: Tier;
  tier3_escalate_threshold: number;
  tier2_escalate_threshold: number;
  persistent_struggle_threshold: number;
  tier1_enabled: boolean;
  tier2_destabilize_threshold: number;
  tier2_destabilize_turns: number;
  tier1_destabilize_threshold: number;
  tier1_destabilize_turns: number;
  force_default_tier: Tier | null;
}

export interface UpdateInput {
  sessionKey: string;
  /** What kind of session this is. See § 2.4 source asymmetry. */
  triggerKind: TriggerKind;
  /** Struggle signal from StruggleDetector for the just-completed turn. */
  struggle?: StruggleSignal;
  /** Triviality signal from TrivialityDetector for the just-completed turn. */
  triviality?: TrivialitySignalShape;
  /** Turn 0-indexed within the session. */
  turnIndex: number;
  /** User-tier override (if explicit choice was set). */
  userRequestedTier?: Tier;
  /** Source of the user-tier choice, if any. Drives de-escalation gating. */
  consentSource?: ConsentSource;
  /** Whether the spend-cap or runaway-rate is forcing tier3 regardless. */
  spendCapForced?: boolean;
  /**
   * Pod-wide pressure flags from the cascade_pressure_watchdog daemon
   * (spec § pressure watchdog).
   *
   * - `pod_tier1_concurrency_cap` / `tier1_pod_spend_burst`: the pod
   *   is over its tier1 limit. Controller must NOT autonomously
   *   escalate to tier1 (ask-hint suppressed; tier2→tier1 background
   *   escalation suppressed). Operator UI-chip Power picks still
   *   honored — those are user choices that pre-empt cascade.
   * - `escalation_storm`: too many cascade-driven escalations in the
   *   recent window. Controller holds current tier (no escalation
   *   OR de-escalation).
   * - `_watchdog_dead` (reader-computed): the watchdog hasn't
   *   updated its heartbeat in >ttl_seconds. Treat as if all flags
   *   were set — operating partly blind, fall back to conservative.
   *
   * Absent (undefined): no pressure data available (brand-new pod,
   * watchdog not installed yet, file unreadable). Treat as no
   * pressure — controller behaves normally. This is the safe
   * default for back-compat with pods that haven't deployed the
   * watchdog daemon yet.
   */
  pressureFlags?: {
    pod_tier1_concurrency_cap?: boolean;
    escalation_storm?: boolean;
    tier1_pod_spend_burst?: boolean;
    _watchdog_dead?: boolean;
  };
  /**
   * Cross-turn struggle signal from SessionStruggleAggregator (added
   * 2026-06-07 after live-pod audit showed real struggle sessions
   * weren't caught by per-turn detectors — the file-copy session had
   * 3 bot self-corrections + 3 shell-error pastes within 6 minutes).
   *
   * When the elevated-threshold helper
   * (``isSessionStruggleElevated()``) returns true on this signal,
   * the controller skips the per-turn persistence requirement and
   * treats THIS turn as having sustained struggle — ask-hint fires
   * immediately. The session-aggregate pattern is itself the
   * persistence evidence; we don't need to count additional turns.
   *
   * Absent: no aggregate data (brand-new session, aggregator not
   * wired). Falls back to per-turn persistence as before.
   */
  sessionAggregate?: {
    shell_error_paste_count: number;
    bot_self_correction_count: number;
    turn_velocity_per_min: number | null;
    turn_count: number;
  };
  /**
   * LLM-as-judge verdict from a prior turn's async judge call (when
   * the aggregator's pre-thresholds tripped — added 2026-06-07 as
   * the sharpening layer on top of the aggregator's wide net).
   *
   * Treated as an additional escalation signal: STRUGGLING lifts
   * effectivePersistentStruggle to the persistence threshold (same
   * as aggregate elevation). OK / AMBIGUOUS / undefined are no-signal
   * (no escalation, no demotion).
   *
   * Architecture: aggregator (cheap regex) is the "wide net"; judge
   * (LLM) is the "look closer at what the net caught." The cascade
   * controller treats their elevations equivalently — either layer
   * firing is enough to escalate.
   */
  sessionJudgeVerdict?: "STRUGGLING" | "OK" | "AMBIGUOUS";
}

/**
 * True when the pressure flags say the pod is over a tier1 limit.
 * Watchdog-dead also returns true (operating blind = be conservative).
 * Internal helper exposed for tests.
 */
export function _shouldBlockTier1(
  pressureFlags: UpdateInput["pressureFlags"],
): boolean {
  if (!pressureFlags) return false;
  return Boolean(
    pressureFlags.pod_tier1_concurrency_cap
    || pressureFlags.tier1_pod_spend_burst
    || pressureFlags._watchdog_dead,
  );
}

/**
 * True when the pressure flags say the controller should hold the
 * current tier (no escalation, no de-escalation). Watchdog-dead OR
 * escalation_storm both trigger hold.
 */
export function _shouldHoldTier(
  pressureFlags: UpdateInput["pressureFlags"],
): boolean {
  if (!pressureFlags) return false;
  return Boolean(
    pressureFlags.escalation_storm || pressureFlags._watchdog_dead,
  );
}

// ── Defaults (spec § 4.3) ────────────────────────────────────────────────────

export const DEFAULT_CASCADE_CONFIG: CascadeControllerConfig = {
  enabled: true,
  user_facing: {
    default_tier: "tier2",
    demote_threshold: 0.7,
    tier3_repromote_threshold: 0.4,
    tier1_ask_enabled: true,
    tier2_struggle_persistence: 2,
    tier2_struggle_threshold: 0.65,
    tier1_ask_cooldown_turns: 10,
    tier1_ask_no_response_turns: 3,
    tier1_destabilize_threshold: 0.2,
    tier1_destabilize_turns: 5,
  },
  background: {
    default_tier: "tier3",
    tier3_escalate_threshold: 0.6,
    tier2_escalate_threshold: 0.75,
    persistent_struggle_threshold: 0.7,
    tier1_enabled: false,
    tier2_destabilize_threshold: 0.2,
    tier2_destabilize_turns: 5,
    tier1_destabilize_threshold: 0.15,
    tier1_destabilize_turns: 5,
    force_default_tier: null,
  },
};

// ── Per-session state ────────────────────────────────────────────────────────

interface SessionState {
  /** Tier this session is currently at. Sticky between turns. */
  currentTier: Tier;
  /** Number of consecutive turns at currentTier. */
  turnsAtCurrentTier: number;
  /** Source the session was first dispatched as (sticky for the session). */
  triggerKind: TriggerKind;
  /** Recent struggle scores for hysteresis + persistence checks. */
  recentStruggle: number[];
  /** Recent triviality scores for demote decisions. */
  recentTriviality: number[];
  /** How many consecutive turns have struggle > tier2_struggle_threshold. */
  persistentStruggleTurns: number;
  /** Last time we emitted a tier1 ask-hint (turn index). null = never. */
  lastAskHintTurn: number | null;
  /** Turns since the last ask-hint without a set_tier("power") agreement. */
  askHintWaitingTurns: number;
}

// ── Helper: is this a "user-facing" session? ─────────────────────────────────

function isUserFacingTrigger(kind: TriggerKind): boolean {
  // Per spec § 2.4: user_turn = user-facing.
  // subagent inherits parent kind — at this scope we'd need the parent's
  // trigger_kind. For Phase 2 scaffold, treat subagent as user-facing
  // (safer default per spec open question #7 — "subagents default to
  // user-facing as the safer fallback").
  // "unknown" arrives when source classification fails (e.g. an OC
  // version bump drops the source/channel fields). Routing it through
  // the background branch would silently force tier3 for live user
  // turns; defaulting to user-facing is the safer fallback (open Q#7).
  return kind === "user_turn" || kind === "subagent" || kind === "unknown";
}

// ── CascadeController ───────────────────────────────────────────────────────

export class CascadeController {
  private readonly config: CascadeControllerConfig;
  private readonly logger: PluginLogger;
  private readonly sessions: Map<string, SessionState>;

  constructor(config: CascadeControllerConfig, logger: PluginLogger) {
    this.config = config;
    this.logger = logger;
    this.sessions = new Map();
  }

  /**
   * Compute the controller's verdict for the NEXT turn, based on the
   * outcome of the just-completed turn (struggle, triviality, etc.).
   *
   * Pure decision logic — no I/O, no side effects on external state.
   * Mutates the controller's per-session state map.
   *
   * Returns the tier the controller would route to, plus an optional
   * ask-hint when sustained struggle warrants asking the user about
   * tier1 escalation.
   */
  decide(input: UpdateInput): CascadeDecision {
    // Lock trigger_kind on first init (spec § 2.4 — sticky across the
    // session). Subsequent turns may carry a different trigger_kind
    // (e.g. a subagent transition or an OC version change that re-labels
    // an in-flight session), but the cascade controller's branch
    // selection must remain stable. Tier history accumulated under one
    // branch must not be re-interpreted by the other.
    const existing = this.sessions.get(input.sessionKey);
    const stickyKind: TriggerKind = existing?.triggerKind ?? input.triggerKind;

    // Initialize per-session state up front so every code path can
    // mutate it. Initial tier depends on source (per spec § 2.4).
    const initialTier: Tier = isUserFacingTrigger(stickyKind)
      ? this.config.user_facing.default_tier
      : (this.config.background.force_default_tier ?? this.config.background.default_tier);
    this._getOrInit(input, initialTier);

    // Spend-cap / runaway-rate force tier3 regardless. HIGHEST precedence
    // — overrides user-requested tier too (a runaway loop ignores even
    // explicit Power consent).
    if (input.spendCapForced) {
      this._updateState(input, "tier3");
      return { tier: "tier3", escalation_event: "held" };
    }

    // Operator-set tier choice next. Bounded by per-bot ceilings in a
    // Phase 3 follow-on; Phase 2 scaffold honors the choice as given.
    if (input.userRequestedTier) {
      this._updateState(input, input.userRequestedTier);
      return { tier: input.userRequestedTier, escalation_event: "held" };
    }

    // Branch by session source per spec § 2.4 — sticky kind, not input's.
    if (isUserFacingTrigger(stickyKind)) {
      return this._chooseTierUserFacing(input);
    }
    return this._chooseTierBackground(input);
  }

  /**
   * Clear all per-session state on session end. Called by TurnObserver.
   */
  clearSession(sessionKey: string): void {
    this.sessions.delete(sessionKey);
  }

  // ── User-facing branch ────────────────────────────────────────────────────

  private _chooseTierUserFacing(input: UpdateInput): CascadeDecision {
    const cfg = this.config.user_facing;
    const state = this._getOrInit(input, cfg.default_tier);

    // Turn 0: start at default tier (typically tier2).
    if (input.turnIndex === 0) {
      this._updateState(input, cfg.default_tier);
      return { tier: cfg.default_tier, escalation_event: "held" };
    }

    // Pressure-gate: when an escalation_storm is in progress (or the
    // watchdog is dead), hold current tier — no escalation, no
    // de-escalation. Returns BEFORE the demote/promote/de-escalate
    // checks below so a pressure event suppresses ALL tier transitions.
    // Ask-hint emission has its own narrower gate further down (only
    // blocks when tier1 specifically would be unreachable).
    if (_shouldHoldTier(input.pressureFlags)) {
      this._updateState(input, state.currentTier);
      return { tier: state.currentTier, escalation_event: "held" };
    }

    const struggle = input.struggle?.score ?? 0;
    const triviality = input.triviality?.score ?? 0;
    // Pre-increment persistent-struggle counter so the check reflects
    // THIS turn's struggle (not the prior turn's). Decisions below read
    // the updated counter; _updateState below re-applies the same logic
    // to keep state in sync for the next call.
    let effectivePersistentStruggle = struggle > cfg.tier2_struggle_threshold
      ? state.persistentStruggleTurns + 1
      : 0;
    // Session-aggregate short-circuit (added 2026-06-07). When cross-
    // turn signals (shell-error pastes, bot self-corrections, turn
    // velocity) are elevated, treat as if persistence had already been
    // met — the aggregate IS the persistence evidence (we've already
    // observed multiple turns of struggle, that's exactly what these
    // counts track). Lifts the counter to the persistence threshold
    // so the ask-hint emission gate below fires this turn. Lazy import
    // to avoid a hard dependency between the controller and the
    // aggregator (the controller is meant to be pure-decision-logic).
    if (input.sessionAggregate) {
      // Inlined threshold check (mirrors SessionStruggleAggregator's
      // isSessionStruggleElevated() — duplicated here so the controller
      // stays independently testable without importing the aggregator).
      const agg = input.sessionAggregate;
      const elevated =
        agg.shell_error_paste_count >= 3
        || agg.bot_self_correction_count >= 2
        || (agg.turn_velocity_per_min !== null
            && agg.turn_velocity_per_min > 0.8
            && agg.turn_count >= 4);
      if (elevated && effectivePersistentStruggle < cfg.tier2_struggle_persistence) {
        effectivePersistentStruggle = cfg.tier2_struggle_persistence;
      }
    }
    // LLM judge verdict short-circuit (2026-06-07 sharpening layer).
    // STRUGGLING is the LLM saying "I read the conversation and yes
    // this user is struggling." Treated equivalently to aggregate
    // elevation — same lift to persistence threshold. OK / AMBIGUOUS
    // are no-signal. The judge runs ONLY when the aggregator's
    // pre-thresholds tripped (caller-side gate); we don't re-check
    // here — if the verdict made it into the input, the LLM already
    // looked at the conversation.
    if (
      input.sessionJudgeVerdict === "STRUGGLING"
      && effectivePersistentStruggle < cfg.tier2_struggle_persistence
    ) {
      effectivePersistentStruggle = cfg.tier2_struggle_persistence;
    }
    // Pre-compute recent-struggle window INCLUDING this turn's value.
    // The user-facing branch uses `effectiveRecent` for the "all-below"
    // destabilize check; recentAvg is only relevant to the background
    // branch's persistent-struggle escalation gate.
    const effectiveRecent = [...state.recentStruggle, struggle].slice(-10);
    // Pre-compute turns-at-current-tier (state's value already reflects
    // last turn; +1 if we're holding).
    const effectiveTurnsAtCurrentTier = state.turnsAtCurrentTier + 1;

    // tier1 sustained-struggle ask-hint emission (does NOT change tier).
    // The bot asks user; if user agrees, set_tier("power") is called
    // separately, which appears as input.userRequestedTier on a future turn.
    //
    // Pressure gate: suppress ask-hint when the pod is at tier1 cap or
    // burst (spec § pressure watchdog). Asking the user "want Power?"
    // when we can't grant Power is bad UX — and if the user says yes,
    // they'd hit the pressure cap on the next turn anyway. Same gate
    // when watchdog_dead — operating blind, be conservative.
    if (
      state.currentTier === "tier2"
      && cfg.tier1_ask_enabled
      && effectivePersistentStruggle >= cfg.tier2_struggle_persistence
      && this._askHintCooldownClear(input.turnIndex, state, cfg)
      && !_shouldBlockTier1(input.pressureFlags)
    ) {
      state.lastAskHintTurn = input.turnIndex;
      state.askHintWaitingTurns = 0;
      this._updateState(input, state.currentTier);
      return {
        tier: state.currentTier,
        askHint: {
          kind: "consider_tier1_escalation",
          struggle_features: input.struggle?.features ?? {},
          struggle_raw: input.struggle?.raw ?? {},
          turns_struggling: effectivePersistentStruggle,
        },
        escalation_event: "held",
      };
    }

    // Demote tier2 → tier3 when work is clearly trivial AND not struggling.
    // Requires POSITIVE EVIDENCE (high triviality + low struggle) — never
    // demote on absence-of-signal alone (spec § 2.5 payload-drift fail-safe).
    // A null score means the detector couldn't measure this turn; coercing
    // to 0 would let demote proceed on unmeasured struggle.
    const struggleMeasured = input.struggle?.score ?? null;
    const trivialityMeasured = input.triviality?.score ?? null;
    if (
      state.currentTier === "tier2"
      && struggleMeasured !== null
      && trivialityMeasured !== null
      && triviality > cfg.demote_threshold
      && struggle < 0.1
    ) {
      this._updateState(input, "tier3");
      return { tier: "tier3", escalation_event: "deescalated" };
    }

    // Re-promote tier3 → tier2 if a previously-trivial conversation
    // turns substantive. Autonomous because the cost step is small
    // (Haiku → Sonnet); no asking required (spec § 2.4 transitions table).
    if (
      state.currentTier === "tier3"
      && struggle > cfg.tier3_repromote_threshold
    ) {
      this._updateState(input, "tier2");
      return { tier: "tier2", escalation_event: "escalated" };
    }

    // Tier1 → tier2 de-escalation (spec § 2.2). Only when consent was
    // via ask-hint (not via UI chip / explicit choice). Requires
    // sustained low struggle.
    if (
      state.currentTier === "tier1"
      && input.consentSource === "ask_hint_agreed"
      && effectiveTurnsAtCurrentTier >= cfg.tier1_destabilize_turns
      && this._allRecentBelow(effectiveRecent, cfg.tier1_destabilize_threshold, cfg.tier1_destabilize_turns)
    ) {
      this._updateState(input, "tier2");
      return { tier: "tier2", escalation_event: "deescalated" };
    }

    // Otherwise: hold current tier.
    this._updateState(input, state.currentTier);
    return { tier: state.currentTier, escalation_event: "held" };
  }

  // ── Background branch ─────────────────────────────────────────────────────

  private _chooseTierBackground(input: UpdateInput): CascadeDecision {
    const cfg = this.config.background;
    // Background default is ALWAYS tier3 (spec § 2.4 hard rule).
    // Per-bot `force_default_tier: tier2` is the single escape hatch for
    // user-visible cron output (morning briefing pattern).
    const turn0Tier: Tier = cfg.force_default_tier ?? cfg.default_tier;
    const state = this._getOrInit(input, turn0Tier);

    if (input.turnIndex === 0) {
      this._updateState(input, turn0Tier);
      return { tier: turn0Tier, escalation_event: "held" };
    }

    // Pressure-gate: escalation_storm OR watchdog_dead → hold tier.
    // Same shape as the user-facing branch — full transition lockout
    // until pressure clears. tier3-escalate AND tier2→tier1 escalate
    // AND de-escalate all blocked.
    if (_shouldHoldTier(input.pressureFlags)) {
      this._updateState(input, state.currentTier);
      return { tier: state.currentTier, escalation_event: "held" };
    }

    const struggle = input.struggle?.score ?? 0;
    // Pre-compute effective values INCLUDING this turn's struggle
    // (same pattern as user-facing branch — fixes the lag-by-one bug).
    const effectiveRecent = [...state.recentStruggle, struggle].slice(-10);
    const recentAvg = this._recentAverage(effectiveRecent);
    const effectiveTurnsAtCurrentTier = state.turnsAtCurrentTier + 1;

    // Escalate tier3 → tier2 on struggle. (No pressure gate here —
    // tier2 is not tier1; the controller can still upgrade to the
    // workhorse model under pod pressure. Only tier1 is the limited
    // resource the watchdog protects.)
    if (
      state.currentTier === "tier3"
      && struggle > cfg.tier3_escalate_threshold
    ) {
      this._updateState(input, "tier2");
      return { tier: "tier2", escalation_event: "escalated" };
    }

    // Escalate tier2 → tier1 only if operator opted in (tier1_enabled)
    // AND sustained struggle AND pod isn't at tier1 cap.
    // Spec § pressure watchdog: cascade must not autonomously add to
    // the tier1 pool when concurrency cap or spend burst is active.
    if (
      state.currentTier === "tier2"
      && cfg.tier1_enabled
      && recentAvg > cfg.tier2_escalate_threshold
      && !_shouldBlockTier1(input.pressureFlags)
    ) {
      this._updateState(input, "tier1");
      return { tier: "tier1", escalation_event: "escalated" };
    }

    // De-escalate tier2 → tier3 with hysteresis. Destabilize threshold
    // (~0.2) much lower than escalate threshold (~0.6); sessions
    // oscillating between hard and easy stay escalated. Sessions that
    // truly recovered free up the cost.
    if (
      state.currentTier === "tier2"
      && effectiveTurnsAtCurrentTier >= cfg.tier2_destabilize_turns
      && this._allRecentBelow(effectiveRecent, cfg.tier2_destabilize_threshold, cfg.tier2_destabilize_turns)
      && recentAvg < cfg.tier2_destabilize_threshold
    ) {
      this._updateState(input, "tier3");
      return { tier: "tier3", escalation_event: "deescalated" };
    }

    // De-escalate tier1 → tier2 (rare path; tier1-enabled bots only).
    if (
      state.currentTier === "tier1"
      && effectiveTurnsAtCurrentTier >= cfg.tier1_destabilize_turns
      && this._allRecentBelow(effectiveRecent, cfg.tier1_destabilize_threshold, cfg.tier1_destabilize_turns)
    ) {
      this._updateState(input, "tier2");
      return { tier: "tier2", escalation_event: "deescalated" };
    }

    // Persistent struggle at max-reachable tier → signal-only.
    // (The signal emission is recorded by TurnObserver into the span;
    // controller doesn't directly write Signals.)
    if (recentAvg > cfg.persistent_struggle_threshold) {
      this._updateState(input, state.currentTier);
      return {
        tier: state.currentTier,
        escalation_event: "held",
      };
    }

    this._updateState(input, state.currentTier);
    return { tier: state.currentTier, escalation_event: "held" };
  }

  // ── State management helpers ──────────────────────────────────────────────

  private _getOrInit(input: UpdateInput, initialTier: Tier): SessionState {
    let state = this.sessions.get(input.sessionKey);
    if (!state) {
      state = {
        currentTier: initialTier,
        turnsAtCurrentTier: 0,
        triggerKind: input.triggerKind,
        recentStruggle: [],
        recentTriviality: [],
        persistentStruggleTurns: 0,
        lastAskHintTurn: null,
        askHintWaitingTurns: 0,
      };
      this.sessions.set(input.sessionKey, state);
    }
    return state;
  }

  private _updateState(input: UpdateInput, newTier: Tier): void {
    const state = this.sessions.get(input.sessionKey);
    if (!state) return;

    const tierChanged = newTier !== state.currentTier;

    if (tierChanged) {
      state.currentTier = newTier;
      state.turnsAtCurrentTier = 1;
    } else {
      state.turnsAtCurrentTier += 1;
    }

    // Slide struggle / triviality windows. Keep last 10 turns; that's
    // enough for de-escalate-on-5-stable checks plus headroom.
    const WINDOW = 10;
    const struggleScore = input.struggle?.score ?? 0;
    state.recentStruggle.push(struggleScore);
    if (state.recentStruggle.length > WINDOW) state.recentStruggle.shift();

    const trivialityScore = input.triviality?.score ?? 0;
    state.recentTriviality.push(trivialityScore);
    if (state.recentTriviality.length > WINDOW) state.recentTriviality.shift();

    // Persistent-struggle counter for ask-hint emission. Counter measures
    // CONSECUTIVE turns of struggle AT THE CURRENT TIER — reset whenever
    // tier changes so a counter that built up at one tier (e.g. while at
    // tier1) can't immediately trigger an ask-hint after we transition
    // back to tier2.
    if (tierChanged) {
      state.persistentStruggleTurns = 0;
    } else {
      const threshold = this.config.user_facing.tier2_struggle_threshold;
      if (struggleScore > threshold) {
        state.persistentStruggleTurns += 1;
      } else {
        state.persistentStruggleTurns = 0;
      }
    }

    // Ask-hint cooldown tracking.
    if (state.lastAskHintTurn !== null) {
      state.askHintWaitingTurns += 1;
      // After tier1_ask_no_response_turns without an agreement, treat
      // as "user declined" and engage cooldown by NOT clearing the
      // lastAskHintTurn (cooldown will check turn distance).
    }
  }

  private _recentAverage(values: number[]): number {
    if (values.length === 0) return 0;
    // Average the last 3 turns (matches spec § 2.2 `recentStruggleAverage`).
    const slice = values.slice(-3);
    return slice.reduce((s, v) => s + v, 0) / slice.length;
  }

  private _allRecentBelow(values: number[], threshold: number, n: number): boolean {
    if (values.length < n) return false;
    const slice = values.slice(-n);
    return slice.every((v) => v < threshold);
  }

  private _askHintCooldownClear(
    currentTurn: number,
    state: SessionState,
    cfg: UserFacingConfig,
  ): boolean {
    if (state.lastAskHintTurn === null) return true;
    // Spec § 2.2: after asking, wait up to `tier1_ask_no_response_turns`
    // for the user to respond. While the ask is pending, suppress
    // unconditionally — the user hasn't had a chance to respond yet, so
    // re-asking would be noise. Once the no-response window has elapsed
    // without agreement, treat the ask as declined and engage the full
    // `tier1_ask_cooldown_turns` cooldown measured from the original ask.
    if (state.askHintWaitingTurns < cfg.tier1_ask_no_response_turns) {
      return false;
    }
    const turnsSinceAsk = currentTurn - state.lastAskHintTurn;
    return turnsSinceAsk >= cfg.tier1_ask_cooldown_turns;
  }
}
