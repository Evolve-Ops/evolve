/**
 * DangerousComboDetector
 *
 * The named-pattern detector for the specific failure mode that
 * motivated the 2026-05-20 cost-alerting blackout (and the broader
 * 2026-05-27 cost-management redesign).
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 2.6.
 *
 * Fires on ANY SINGLE TURN matching ALL FOUR features:
 *
 *   1. Origin: background-pure (trigger_kind ∈ {heartbeat, cron_app})
 *      — no human in the loop, output never read by a user
 *   2. Tier: tier1 (Opus, ~5-10× tier2 cost)
 *   3. Source of tier choice: cascade-decided
 *      — NOT operator's UI-chip Power, NOT bot_initiated
 *      — the SYSTEM decided to spend this money, not the user
 *   4. Context size: large (>100K tokens, configurable)
 *
 * Single-occurrence trigger. No baseline accumulation, no waiting for
 * N consecutive turns. ONE turn matching all four = `cascade_dangerous_combo`
 * Signal at WARNING. The operator hears about it immediately.
 *
 * The pattern that motivated this design: cron job fires → cascade
 * escalates tier3→tier2→tier1 because of struggle signal → huge context
 * (e.g., model is reading all of a user's docs to answer a question)
 * → silent expensive turn the operator never sees until the bill lands.
 *
 * No baseline needed. The detector encodes the canonical "bad cost
 * combination" rather than waiting to detect it as an anomaly. Symbiotic
 * with the anomaly detector (Phase 2+) — anomaly catches statistical
 * outliers; dangerous-combo catches a known-named pattern.
 *
 * Pure function, no I/O. Data-independent (no Phase 1 telemetry needed
 * to build/ship).
 */
export interface DangerousComboInput {
    /**
     * Session source per spec § 2.4 taxonomy. The detector matches when
     * triggerKind is in `backgroundTriggerKinds` config (default:
     * heartbeat, cron_app).
     */
    triggerKind?: string;
    /** Tier the model actually billed at this turn. Detector matches on tier1. */
    tierUsed?: string | null;
    /**
     * How the tier got picked. Detector matches when this is "cascade"
     * (autonomous decision) — explicitly NOT when "user_request" or
     * "bot_initiated" or "ui_chip" (operator made the call).
     */
    tierChosenBy?: string;
    /**
     * Total input context size for this turn. Detector matches when this
     * exceeds `minContextTokens` (default 100,000). Use input_tokens from
     * the llm_output hook payload — that's the model's input context,
     * which is what costs money on Anthropic pricing.
     */
    contextTokens?: number;
}
export interface DangerousComboConfig {
    enabled?: boolean;
    /** Which trigger_kind values count as "background pure." */
    backgroundTriggerKinds?: string[];
    /** Which tier_used value counts as "expensive." */
    tier?: string;
    /** Which tier_chosen_by values count as "system decided, not user." */
    cascadeChosenBySet?: string[];
    /** Threshold for "large context" in input tokens. */
    minContextTokens?: number;
}
export interface DangerousComboResult {
    matched: boolean;
    /**
     * Which of the four required features were observed. Useful for the
     * audit layer / Signal payload to explain WHICH parts of the pattern
     * fired even when overall not matched (near-misses are worth knowing
     * for tuning the detector).
     */
    features_matched: {
        background_origin: boolean;
        tier1: boolean;
        cascade_chosen: boolean;
        large_context: boolean;
    };
    /** When matched, the input context size that tripped the detector. */
    context_tokens?: number;
}
export declare const DEFAULT_DANGEROUS_COMBO_CONFIG: Required<DangerousComboConfig>;
/**
 * Check whether the input matches the dangerous-combo pattern. Pure
 * function — no I/O, no side effects, deterministic.
 *
 * Returns matched=true when ALL FOUR features match. Always returns
 * features_matched so callers can analyze near-misses.
 *
 * Failure mode: if `cfg.enabled === false`, always returns matched=false
 * (regardless of feature state). Allows operator to disable without
 * removing the detector from the code path.
 */
export declare function detectDangerousCombo(input: DangerousComboInput, config?: DangerousComboConfig): DangerousComboResult;
//# sourceMappingURL=DangerousComboDetector.d.ts.map