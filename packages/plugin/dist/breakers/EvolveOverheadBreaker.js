/**
 * Reader for the Evolve overhead breaker (D-OH5).
 *
 *     {sharedDir}/breakers/<bot_id>/evolve_overhead.json
 *
 * Written by Python only; the plugin never writes it. File existence means
 * tripped. This breaker can only REMOVE Evolve machinery (subagent runs),
 * never a conversation, so unknown state fails toward doing less Evolve
 * work: a file that exists but is unreadable counts as TRIPPED.
 */
import * as fs from "fs";
import * as path from "path";
import { isExpired } from "./BreakerStateReader.js";
export function readEvolveOverheadDecision(opts) {
    const unreadable = (why) => ({ tripped: true, unreadable: true, reason: why });
    try {
        const p = path.join(opts.sharedDir, "breakers", opts.botId, "evolve_overhead.json");
        if (!fs.existsSync(p))
            return { tripped: false, unreadable: false, reason: "" };
        let data;
        try {
            data = JSON.parse(fs.readFileSync(p, { encoding: "utf-8" }));
        }
        catch {
            return unreadable("unreadable or unparseable JSON");
        }
        if (typeof data !== "object" || data === null || Array.isArray(data)) {
            return unreadable("not a JSON object");
        }
        const rec = data;
        // Lenient on fields: presence of the file is the trip; only expiry can lift it.
        const record = {
            bot_id: typeof rec.bot_id === "string" ? rec.bot_id : opts.botId,
            type: "evolve_overhead",
            state: "tripped",
            tripped_at: typeof rec.tripped_at === "string" ? rec.tripped_at : "",
            expires_at: typeof rec.expires_at === "string" ? rec.expires_at : null,
            initiated_by: "unknown",
            reason: typeof rec.reason === "string" ? rec.reason : "",
            trip_id: typeof rec.trip_id === "string" ? rec.trip_id : undefined,
        };
        if (isExpired(record, opts.now ?? new Date())) {
            return { tripped: false, unreadable: false, reason: "" };
        }
        return { tripped: true, unreadable: false, reason: record.reason, tripId: record.trip_id };
    }
    catch (err) {
        return unreadable(`overhead breaker state unreadable: ${err?.message ?? err}`);
    }
}
/** Cheap existence probe used by per-turn early returns. */
export function evolveOverheadTripped(opts) {
    try {
        if (!fs.existsSync(path.join(opts.sharedDir, "breakers", opts.botId, "evolve_overhead.json"))) {
            return false;
        }
    }
    catch {
        return true;
    }
    return readEvolveOverheadDecision(opts).tripped;
}
//# sourceMappingURL=EvolveOverheadBreaker.js.map