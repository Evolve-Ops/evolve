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
import * as fs from "fs";
import * as path from "path";
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
export function readBreakerFile(p) {
    let text;
    try {
        if (!fs.existsSync(p)) {
            return null;
        }
        text = fs.readFileSync(p, { encoding: "utf-8" });
    }
    catch {
        return null;
    }
    let data;
    try {
        data = JSON.parse(text);
    }
    catch {
        return null;
    }
    if (!isPlainObject(data)) {
        return null;
    }
    // Type-narrow minimally; missing required fields → return null
    // (treat as "no usable trip" rather than risk a partial record).
    const rec = data;
    if (typeof rec.bot_id !== "string"
        || typeof rec.type !== "string"
        || typeof rec.tripped_at !== "string") {
        return null;
    }
    return {
        bot_id: rec.bot_id,
        type: rec.type,
        state: typeof rec.state === "string" ? rec.state : "tripped",
        tripped_at: rec.tripped_at,
        expires_at: typeof rec.expires_at === "string" ? rec.expires_at : null,
        initiated_by: typeof rec.initiated_by === "string" ? rec.initiated_by : "unknown",
        reason: typeof rec.reason === "string" ? rec.reason : "",
        trip_id: typeof rec.trip_id === "string" ? rec.trip_id : undefined,
    };
}
/**
 * Has this trip expired? Mirrors Python is_expired().
 *
 * - expires_at === null → never expires (indefinite trip)
 * - expires_at unparseable → treat as active (fail-SAFE here, not fail-OPEN;
 *   a malformed expiry shouldn't accidentally clear a trip)
 * - expires_at in the past → expired
 */
export function isExpired(rec, now = new Date()) {
    if (rec.expires_at === null || rec.expires_at === undefined) {
        return false;
    }
    const exp = parseIso(rec.expires_at);
    if (exp === null) {
        return false; // malformed — assume still active
    }
    return now.getTime() >= exp.getTime();
}
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
export function readCostBreakerDecision(opts) {
    const now = opts.now ?? new Date();
    const breakersRoot = path.join(opts.sharedDir, "breakers");
    // Per-bot file first — its reason tends to be more specific.
    const perBotPath = path.join(breakersRoot, opts.botId, "cost.json");
    const perBot = readBreakerFile(perBotPath);
    if (perBot !== null && !isExpired(perBot, now)) {
        return {
            vetoed: true,
            scope: "bot",
            reason: perBot.reason,
            tripId: perBot.trip_id,
        };
    }
    // Pod-wide fallback.
    const podPath = path.join(breakersRoot, "pod", "cost.json");
    const pod = readBreakerFile(podPath);
    if (pod !== null && !isExpired(pod, now)) {
        return {
            vetoed: true,
            scope: "pod",
            reason: pod.reason,
            tripId: pod.trip_id,
        };
    }
    return { vetoed: false };
}
// ── internals ────────────────────────────────────────────────────────────────
function isPlainObject(x) {
    return typeof x === "object" && x !== null && !Array.isArray(x);
}
function parseIso(s) {
    // Date.parse accepts ISO-8601 with or without trailing Z. Returns NaN
    // on garbage. Same conservative parsing as the Python side.
    const t = Date.parse(s);
    if (Number.isNaN(t)) {
        return null;
    }
    return new Date(t);
}
//# sourceMappingURL=BreakerStateReader.js.map