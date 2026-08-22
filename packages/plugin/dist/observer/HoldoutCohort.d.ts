/**
 * HoldoutCohort — the un-cascaded reference cohort.
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 2.3 Component 5.
 *
 * 2% of sessions are deterministically assigned to a "holdout cohort"
 * that runs on a fixed baseline policy (the keyword classifier during
 * Phase 1-3; the pre-cascade default once Phase 3 retires the
 * classifier). The Phase 4 audit layer's tuning loop compares
 * cascade-affected outcomes against holdout outcomes — without this
 * un-contaminated reference, the learning loop trains on its own
 * decisions and becomes self-confirming.
 *
 * Per the 2026-05-26 round-2 stress-test review (tuning F1+F2+F3+F8,
 * all four converged on this single mitigation). Per the round-3
 * clarifications (finding #1, #5, #6) — three rules baked in:
 *
 *   1. Hijacked holdouts (UI-chip / user-initiated overrides on a
 *      holdout-assigned session) are excluded from the tuner — tagged
 *      `holdout_hijacked` instead of `holdout`. This file only handles
 *      the assignment; hijack-tagging happens at span-write time.
 *
 *   2. Subagent inheritance: subagents inherit the parent session's
 *      holdout flag, NOT independently hashed. A holdout-assigned
 *      parent spawning 7 subagents would otherwise be incoherent
 *      (parent's outcome shaped by children's cascade decisions).
 *
 *   3. Variant tagging: holdout spans carry `cascade.variant: "baseline"`
 *      (distinct value, not "A"). Phase 2/3 telemetry code uses this.
 *
 * **Phase 2 (this PR) — assignment-only.** Cascade isn't yet driving
 * routing, so holdout-assigned sessions don't yet RUN differently from
 * non-holdout sessions. But tagging spans starting NOW means when
 * Phase 3 cuts cascade live, we have a corpus of pre-cascade-baseline-
 * behavior data already accumulated. Auto-bootstrap pattern: ship
 * safe code that activates when downstream wiring lands.
 *
 * Pure function. No I/O. Deterministic from (bot_id, session_id) hash.
 */
export interface HoldoutCohortConfig {
    /** Master switch. When false, no session is ever in holdout. */
    enabled?: boolean;
    /**
     * Target percentage of sessions in the cohort (0.0 - 1.0). Default 0.02
     * (2%). Spec hard ceiling: 0.05. Operator override beyond 0.05 emits
     * `cascade_holdout_oversized` Signal but is honored — the cap is
     * advisory, not enforced.
     */
    target_rate?: number;
    /** Hard ceiling enforced via `cascade_holdout_oversized` Signal. */
    oversized_threshold?: number;
}
export declare const DEFAULT_HOLDOUT_CONFIG: Required<HoldoutCohortConfig>;
/**
 * Decide whether a given session should be in the holdout cohort.
 *
 * Inputs: bot_id, session_id, config. Output: boolean.
 *
 * Pure function. Same (bot_id, session_id) inputs ALWAYS produce the
 * same answer — restart-safe, replay-safe. Config changes affect new
 * sessions, not existing ones (since this is called once at session
 * start).
 *
 * Subagent inheritance is NOT handled here — callers wanting subagent
 * inheritance should use `inheritFromParent` instead, or call this with
 * the parent's session_id (so child and parent hash to the same value).
 */
export declare function shouldBeInHoldout(botId: string, sessionId: string, config?: HoldoutCohortConfig): boolean;
/**
 * Subagent inheritance rule (spec § 2.3 Component 5, round-3 finding #6):
 * subagents inherit their parent session's holdout assignment, NOT
 * independently hashed. Otherwise a holdout-assigned parent spawning K
 * children would have some children running baseline policy and some
 * running cascade — making the parent's outcome incoherent.
 *
 * This is a one-liner because the rule IS inheritance, but exists as a
 * named function so the call site documents intent.
 */
export declare function inheritFromParent(parentInHoldout: boolean): boolean;
/**
 * Check if the actual cohort rate is over the spec's hard ceiling
 * (default 5%). Used by the audit layer to emit
 * `cascade_holdout_oversized` Signal when an operator override pushes
 * past the recommended cap.
 *
 * Returns `{ oversized: bool, observed_rate: number, threshold: number }`.
 */
export declare function checkOversized(observedRate: number, config?: HoldoutCohortConfig): {
    oversized: boolean;
    observed_rate: number;
    threshold: number;
};
//# sourceMappingURL=HoldoutCohort.d.ts.map