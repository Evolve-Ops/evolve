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
export declare const PLUGIN_VERSION = "0.1.0";
/** One appended line's shape — matches what breakers.store expects. */
export interface HoldMarkerRow {
    readonly trip_id: string;
    readonly ts: string;
    readonly session: string | null;
    readonly plugin_version: string;
}
/**
 * Append one "held" marker for ``tripId``. No-op when ``tripId`` is empty
 * (an unreadable breaker record carries none — nothing to evidence). Never
 * throws.
 */
export declare function recordInteractiveHold(opts: {
    sharedDir: string;
    botId: string;
    tripId: string;
    session: string | null;
    pluginVersion: string;
    logger?: {
        warn: (msg: string) => void;
    };
    now?: Date;
}): void;
//# sourceMappingURL=HoldMarker.d.ts.map