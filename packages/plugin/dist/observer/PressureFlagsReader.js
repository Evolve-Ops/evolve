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
import * as fs from "fs";
import * as path from "path";
const DEFAULT_TTL_SECONDS = 180;
const CACHE_TTL_MS = 30_000; // 30s — well under the 60s watchdog cadence
export class PressureFlagsReader {
    filePath;
    _cached = null;
    constructor(sharedDir) {
        this.filePath = path.join(sharedDir, "cascade", "pressure_flags.json");
    }
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
    read(nowMs = Date.now()) {
        if (this._cached && nowMs - this._cached.at < CACHE_TTL_MS) {
            return this._cached.flags;
        }
        let flags = null;
        try {
            if (fs.existsSync(this.filePath)) {
                const raw = fs.readFileSync(this.filePath, "utf8");
                flags = JSON.parse(raw);
                // Check heartbeat freshness. The watchdog writes its heartbeat
                // as ISO-8601 UTC on every poll. Stale = >watchdog_ttl_seconds
                // (default 180s = three missed polls at 60s cadence).
                const ttl = (flags.watchdog_ttl_seconds ?? DEFAULT_TTL_SECONDS) * 1000;
                const hbMs = new Date(flags.watchdog_heartbeat ?? "").getTime();
                if (!Number.isFinite(hbMs)) {
                    // Heartbeat field missing or unparsable → treat as dead.
                    // Stay conservative (a future watchdog version with a new
                    // heartbeat shape would silently un-gate the controller
                    // otherwise).
                    flags._watchdog_dead = true;
                }
                else if (nowMs - hbMs > ttl) {
                    flags._watchdog_dead = true;
                }
            }
        }
        catch {
            // File missing / malformed / EACCES → caller treats null as
            // "no pressure data." Fail open.
            flags = null;
        }
        this._cached = { flags, at: nowMs };
        return flags;
    }
    /**
     * Drop the cache. Test-only — production code relies on the TTL.
     */
    _invalidateCache() {
        this._cached = null;
    }
}
//# sourceMappingURL=PressureFlagsReader.js.map