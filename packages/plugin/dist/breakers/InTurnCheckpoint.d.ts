/**
 * In-turn cost checkpoint — D-CC1..4 reach inside one run (D-CS12, ratified
 * 2026-09-22 on the $65.98 / 304-call turn in internal/incident-post-mortem-
 * 2026-09-20-image-turn-cache-thrash-and-poll-loop.md). Damage control; the
 * structural fixes (no-progress veto, image-turn cache thrash) still come first.
 *
 * At ``inTurnCheckpointUsd`` of recorded spend in one run, the ONE
 * before_tool_call gate (ToolCallGate) vetoes the next tool call with a fixed
 * message and every later one with a one-liner, so the run ends on the
 * model's next reply — held, never degraded, no retry (D-CC2). The owner
 * notice (CostCheckpoint.ts, deterministic) is appended to that reply once
 * per run via ``reply_payload_sending`` (the only outbound hook carrying the
 * runId) — the conversation, not the notify path, which is broken until
 * ``breaker-notify-survives-openclaw-config-validation`` lands. An owner's
 * "continue" raises the next run's checkpoint by D-CC3's +50 %, ledgered.
 *
 * Positive evidence only (D-CS7): no runId → ``unknown``, allowed. Spend is
 * catalog-priced (``estimateCost``; D-CS2 not landed), so it is "recorded".
 * Writes {sharedDir}/breakers/<bot>/in-turn-checkpoints.jsonl (trip / notice /
 * continue rows) and in-turn-checkpoint-status.json (the health control's
 * armed + liveness counters).
 */
export declare const IN_TURN_CHECKPOINT_DEFAULT_USD = 5;
/** Config can raise the checkpoint, never lower it below this. */
export declare const IN_TURN_CHECKPOINT_FLOOR_USD = 1;
export declare const IN_TURN_PRICE_SOURCE = "catalog";
/** ``inTurnCheckpointUsd`` from plugin config; below the floor → refused. */
export declare function resolveInTurnCheckpointUsd(pluginConfig: Record<string, unknown>): {
    usd: number;
    warning: string | null;
};
/** The first veto's reason — the model reads it in place of the tool result. */
export declare function renderInTurnVeto(spendUsd: number): string;
/** Every later veto in the same run. */
export declare const IN_TURN_VETO_REPEAT = "Evolve checkpoint: tool calls are stopped for this turn \u2014 reply to the user now.";
export type InTurnDecision = {
    kind: "unknown";
} | {
    kind: "allow";
    spendUsd: number;
} | {
    kind: "veto";
    first: boolean;
    spendUsd: number;
    reason: string;
};
interface Logger {
    info(m: string): void;
    warn(m: string): void;
}
/** Per-run spend + the decision; singleton ``inTurnCheckpoint``, fed by
 *  TurnObserver's llm_output and read by ToolCallGate's before_tool_call. */
export declare class InTurnCheckpoint {
    private runs;
    /** Sessions whose last run tripped — the only place "continue" means this. */
    private trippedSessions;
    private sharedDir;
    private botId;
    private thresholdUsd;
    private registered;
    private logger;
    private counters;
    private lastStatusMs;
    private warnedUnknown;
    configure(opts: {
        sharedDir: string;
        botId: string;
        thresholdUsd: number;
        registered: boolean;
        logger?: Logger;
    }): void;
    private run;
    /** One llm_output's catalog-priced cost, keyed by run. */
    recordCost(runId: string | null | undefined, sessionId: string | null, costUsd: number, now?: Date): void;
    /** The before_tool_call decision. */
    evaluate(runId: string | null | undefined, sessionId: string | null, now?: Date): InTurnDecision;
    /** Once per tripped run: the owner notice's facts; records the delivery. */
    takeOwnerNotice(runId: string | null | undefined, now?: Date): {
        spendUsd: number;
        thresholdUsd: number;
        incrementUsd: number;
        llmCalls: number;
        toolCalls: number;
    } | null;
    /** D-CC3 grammar: +50 % of the BASE checkpoint. */
    incrementUsd(): number;
    /** before_agent_run: an owner's "continue" after a trip on this session
     *  raises THIS run's checkpoint; a non-owner's is an ordinary message. */
    noteTurnStart(runId: string | null | undefined, sessionId: string | null, message: string, now?: Date): void;
    /** agent_end: marked, not dropped — OC does not order agent_end against
     *  the last reply payload, and the notice must still find its run. */
    endRun(runId: string | null | undefined, now?: Date): void;
    /** A trip whose notice never went out (NO_REPLY, no text) is ledgered. */
    private drop;
    private prune;
    private dir;
    private append;
    /** The health control's input. Throttled; atomic tmp+rename. */
    private writeStatus;
}
/** The gateway's one ledger. */
export declare const inTurnCheckpoint: InTurnCheckpoint;
/** ``reply_payload_sending`` wrapper: append the owner notice to a stopped
 *  run's first text payload, over whatever the other handlers decided. */
export declare function withInTurnOwnerNotice(event: {
    runId?: unknown;
    payload?: {
        text?: unknown;
    };
} | null | undefined, result: {
    payload?: any;
} | undefined, ledger?: InTurnCheckpoint): {
    payload?: any;
} | undefined;
export {};
//# sourceMappingURL=InTurnCheckpoint.d.ts.map