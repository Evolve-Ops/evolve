/**
 * Interactive-hold evidence marker (D-CS13;
 * internal/decision-cost-single-turn-2026-09-18.md).
 *
 * On the first interactive hold after a cost-checkpoint trip
 * (``TurnObserver._breakerReplyClaim``'s "first blocked turn of this trip
 * in this conversation" branch), the plugin appends one line here. This is
 * its OWN file — the plugin never writes ``cost.json`` (that stays
 * ``breakers.store``'s alone; see ``CostCheckpoint.ts``'s header). The
 * Python side reads this file directly (``breakers.store.read_earliest_hold``)
 * and folds the earliest matching line into the trip's ``cost.json`` as
 * ``first_hold_at`` / ``hold_plugin_version`` on its next write.
 *
 * Written from the 2026-09-17 incident: a cap trip held ``pending`` while
 * $4.15 was spent on the operator's DM in the next seven minutes, and
 * nothing on disk said whether the plugin live at that minute carried the
 * hold. This file is that evidence.
 *
 * Best-effort, matching CascadeTelemetry's shape: a failed append costs
 * only the evidence field (the admin health control reads it as
 * "unknown", never a crash), never the hold behaviour itself.
 */
import * as fs from "fs";
import * as path from "path";
// Placeholder, mirroring api/routes.ts's own PLUGIN_VERSION (not imported
// from there — routes.ts pulls in TurnObserver.ts transitively via
// networkRoutes.ts, and this module is imported BY TurnObserver.ts, so a
// cross-import would be circular). Sync both by hand on a version bump.
export const PLUGIN_VERSION = "0.1.0";
let warnedEACCES = false;
/**
 * Append one "held" marker for ``tripId``. No-op when ``tripId`` is empty
 * (an unreadable breaker record carries none — nothing to evidence). Never
 * throws.
 */
export function recordInteractiveHold(opts) {
    if (!opts.tripId)
        return;
    const dir = path.join(opts.sharedDir, "breakers", opts.botId);
    const row = {
        trip_id: opts.tripId,
        ts: (opts.now ?? new Date()).toISOString(),
        session: opts.session || null,
        plugin_version: opts.pluginVersion,
    };
    try {
        fs.mkdirSync(dir, { recursive: true });
        fs.appendFileSync(path.join(dir, "holds.jsonl"), JSON.stringify(row) + "\n", { mode: 0o644 });
    }
    catch (err) {
        // Fail-open, once-per-process warning — same shape as
        // CascadeTelemetry's mkdir/append guard. A missing marker degrades
        // the health control to "unknown"; it must never brick a hold.
        if ((err?.code === "EACCES" || err?.code === "EPERM") && !warnedEACCES) {
            warnedEACCES = true;
            opts.logger?.warn(`Evolve cost checkpoint: cannot record interactive-hold evidence ` +
                `at ${dir} (${err.code}) — the admin health control will read this ` +
                `trip as "unknown" until the shared-dir permissions are fixed. ` +
                `(Warning fires once per process.)`);
        }
        else if (err?.code !== "EACCES" && err?.code !== "EPERM") {
            opts.logger?.warn(`Evolve cost checkpoint: failed to record interactive-hold ` +
                `evidence: ${err?.message ?? err}`);
        }
    }
}
//# sourceMappingURL=HoldMarker.js.map