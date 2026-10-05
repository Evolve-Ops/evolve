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
export interface EvolveOverheadDecision {
    tripped: boolean;
    unreadable: boolean;
    reason: string;
    tripId?: string;
}
export declare function readEvolveOverheadDecision(opts: {
    sharedDir: string;
    botId: string;
    now?: Date;
}): EvolveOverheadDecision;
/** Cheap existence probe used by per-turn early returns. */
export declare function evolveOverheadTripped(opts: {
    sharedDir: string;
    botId: string;
}): boolean;
//# sourceMappingURL=EvolveOverheadBreaker.d.ts.map