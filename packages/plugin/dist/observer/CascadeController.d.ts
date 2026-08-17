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
export type Tier = "tier0" | "tier1" | "tier2" | "tier3";
export type TriggerKind = "user_turn" | "heartbeat" | "cron_app" | "subagent" | "summarizer" | "classifier" | "task_extractor" | "fallback" | "unknown";
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
export declare function _shouldBlockTier1(pressureFlags: UpdateInput["pressureFlags"]): boolean;
/**
 * True when the pressure flags say the controller should hold the
 * current tier (no escalation, no de-escalation). Watchdog-dead OR
 * escalation_storm both trigger hold.
 */
export declare function _shouldHoldTier(pressureFlags: UpdateInput["pressureFlags"]): boolean;
export declare const DEFAULT_CASCADE_CONFIG: CascadeControllerConfig;
export declare class CascadeController {
    private readonly config;
    private readonly logger;
    private readonly sessions;
    constructor(config: CascadeControllerConfig, logger: PluginLogger);
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
    decide(input: UpdateInput): CascadeDecision;
    /**
     * Clear all per-session state on session end. Called by TurnObserver.
     */
    clearSession(sessionKey: string): void;
    private _chooseTierUserFacing;
    private _chooseTierBackground;
    private _getOrInit;
    private _updateState;
    private _recentAverage;
    private _allRecentBelow;
    private _askHintCooldownClear;
}
export {};
//# sourceMappingURL=CascadeController.d.ts.map