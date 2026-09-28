/**
 * PressureFlagsReader — read the pod-wide pressure flag bundle that
 * the cascade_pressure_watchdog daemon writes to
 * `{sharedDir}/cascade/pressure_flags.json`.
 *
 * Why this module exists separately:
 *   - CascadeController.decide() is a pure function (same inputs →
 *     same outputs). It shouldn't do file I/O.
 *   - The watchdog daemon updates pressure_flags.json every 60s.
 *   - The plugin's per-turn hot path can't afford to re-read the file
 *     on every `decide()` call (a busy bot is 10+ turns/sec across
 *     parallel sessions; 60s file write rate × per-turn read rate is
 *     ~600× overhead for no information gain).
 *
 * Solution: TurnObserver instantiates one of these per bot, reads
 * once per turn with a short in-memory TTL (30s), and passes the
 * latest flags into CascadeController.decide() via UpdateInput.
 *
 * Failure modes:
 *   - File missing entirely → return null. CascadeController treats
 *     null as "no pressure data; behave normally." Conservative
 *     fallback is the responsibility of the caller checking the
 *     heartbeat freshness.
 *   - File present but stale heartbeat (>watchdog_ttl_seconds old) →
 *     return flags with `_watchdog_dead: true`. CascadeController
 *     reads this and applies conservative defaults (treat as if all
 *     pressure flags fired).
 *   - File present, malformed JSON → return null. Atomic writes
 *     (tmp+rename) mean we shouldn't see torn reads; a malformed
 *     file is operator-induced + already broken.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § pressure watchdog.
 */
export interface PressureFlags {
    pod_tier1_concurrency_cap: boolean;
    pod_tier1_active_sessions: number;
    escalation_storm: boolean;
    escalations_in_15min: number;
    live_escalations_in_15min: number;
    tier1_pod_spend_burst: boolean;
    tier1_pod_spend_per_hour_usd: number;
    pressure_event_id: string | null;
    telemetry_partially_lost: boolean;
    effective_concurrency_cap: number;
    watchdog_heartbeat: string;
    watchdog_ttl_seconds: number;
    /**
     * Reader-computed: true when the watchdog's heartbeat is older than
     * `watchdog_ttl_seconds` (default 180s). When true, the controller
     * MUST behave as if all pressure flags were set (conservative
     * fallback — we're routing partly blind). Not present in the
     * file itself; added by the reader.
     */
    _watchdog_dead?: boolean;
}
export declare class PressureFlagsReader {
    private readonly filePath;
    private _cached;
    constructor(sharedDir: string);
    /**
     * Read the current pressure flags, with in-memory cache.
     *
     * Returns null when the file doesn't exist OR can't be parsed —
     * caller (CascadeController) treats null as "no pressure data"
     * (= no pressure; behave normally). This is the safe default for
     * a brand-new pod or a pod that hasn't installed the watchdog
     * daemon yet.
     *
     * When the file IS readable but the heartbeat is stale, returns
     * the flags with `_watchdog_dead: true` so the controller can
     * apply conservative defaults.
     */
    read(nowMs?: number): PressureFlags | null;
    /**
     * Drop the cache. Test-only — production code relies on the TTL.
     */
    _invalidateCache(): void;
}
//# sourceMappingURL=PressureFlagsReader.d.ts.map