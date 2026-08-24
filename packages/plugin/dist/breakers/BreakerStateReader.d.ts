/**
 * Reads L1 (cost) breaker state from the shared-dir file layout.
 *
 * Spec: internal/spec-circuit-breakers-2026-05-21.md §5.3
 *
 * File layout (Python writers — packages/analyzer/breakers/store.py +
 * packages/admin/evolve_admin/breakers_enforce.py):
 *
 *     {sharedDir}/breakers/<bot_id>/cost.json    — per-bot L1
 *     {sharedDir}/breakers/pod/cost.json         — pod-wide L1
 *
 * The plugin only needs to READ these. It is never the writer. Schema
 * is documented in the Python store; we deserialize what we need and
 * ignore extra fields. Fail-open at every error path.
 *
 * Performance: this runs on every non-user-channel turn via
 * before_agent_run. A local-disk read of a tiny JSON is sub-millisecond,
 * so no caching layer in v1 — keep the code simple and predictable.
 * Add caching only if profiling shows it matters.
 */
/**
 * Minimal subset of the breaker record schema we care about for veto
 * decisions. The Python writer includes additional fields (trip_id,
 * motivating_signals, audit_summary, etc.) that we deserialize but
 * don't act on here. Forward-compatible to additional fields.
 */
export interface BreakerRecord {
    bot_id: string;
    type: string;
    state: string;
    tripped_at: string;
    expires_at: string | null;
    initiated_by: string;
    reason: string;
    trip_id?: string;
}
export interface VetoDecision {
    vetoed: boolean;
    /** "bot" if the per-bot breaker tripped; "pod" if the pod-wide one tripped. */
    scope?: "bot" | "pod";
    reason?: string;
    tripId?: string;
}
/**
 * Read raw breaker JSON from disk. Returns null when the file is
 * missing, unreadable, or unparseable. Never throws.
 *
 * Fail-open property: a corrupt or truncated file MUST NOT block
 * a turn. We return null exactly as if no trip exists. Mirrors
 * heal.py's defensive read of pause-state.json. Better to let a
 * possibly-vetoable turn through on a parsing glitch than to brick
 * the bot.
 */
export declare function readBreakerFile(p: string): BreakerRecord | null;
/**
 * Has this trip expired? Mirrors Python is_expired().
 *
 * - expires_at === null → never expires (indefinite trip)
 * - expires_at unparseable → treat as active (fail-SAFE here, not fail-OPEN;
 *   a malformed expiry shouldn't accidentally clear a trip)
 * - expires_at in the past → expired
 */
export declare function isExpired(rec: BreakerRecord, now?: Date): boolean;
/**
 * Resolve the L1 cost-breaker decision for this bot.
 *
 * Returns vetoed=true if EITHER the per-bot breaker OR the pod-wide
 * breaker is tripped and not expired. scope/reason/tripId populated
 * from whichever breaker is active. Per-bot wins over pod-wide if
 * both are active (the more specific scope tends to carry the
 * relevant reason).
 *
 * No caching in v1 — sub-ms reads of two small JSON files on every
 * non-user-channel turn is fine. Add a cache if profiling says
 * otherwise.
 */
export declare function readCostBreakerDecision(opts: {
    sharedDir: string;
    botId: string;
    now?: Date;
}): VetoDecision;
//# sourceMappingURL=BreakerStateReader.d.ts.map