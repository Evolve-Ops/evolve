/**
 * No-progress gate — bounds repetition inside one run, without a price
 * (internal/decision-cost-single-turn-2026-09-18.md §2–3; acceptance case: the
 * 171 byte-identical ``subagents list`` calls of internal/incident-post-mortem-
 * 2026-09-20-image-turn-cache-thrash-and-poll-loop.md).
 *
 * A per-run ledger in memory, discarded at agent_end, fed by the ONE
 * before_tool_call gate (ToolCallGate: calls) and the tool-result middleware
 * (AppIntegrityMiddleware: every result, with its runId and host error / exit
 * status, in-turn). Positive evidence only; no runId → ``unknown``, allowed:
 *   (a) identical call — the same ``(toolName, key-sorted arguments)`` hash a
 *       3rd time in one run is vetoed. Arguments alone: no read tool is exempt,
 *       and ``subagents list`` returns a changing ``runtime`` every call.
 *   (b) failing validation — the same tool failing with the same result 3
 *       times vetoes the next call. Keyed on the failure, not the arguments:
 *       with identical arguments (a) stops the 3rd call before it has a result.
 *   (c) D-CS11 — ``maxModelCallsPerRun`` (plugin config; 0 = off): a tool call
 *       after more model calls than that is vetoed.
 * Once fired, every later tool call in the run gets a one-liner, so the run
 * ends on the model's next reply. Never a model change, never a retry (D-CC2).
 * Writes {sharedDir}/breakers/<bot>/no-progress.jsonl (a ``no_progress`` row
 * per stopped run) and no-progress-status.json (the health control's input,
 * read by packages/analyzer/breakers/no_progress.py).
 */
export declare const NO_PROGRESS_IDENTICAL_CALLS = 3;
export declare const NO_PROGRESS_FAILURES = 3;
export type NoProgressPredicate = "identical_call" | "failing_validation" | "call_ceiling";
/** ``maxModelCallsPerRun`` from plugin config: unset or 0 → off (the shipped
 *  default is 0); else a whole number ≥ 1. */
export declare function resolveMaxModelCallsPerRun(pluginConfig: Record<string, unknown>): {
    max: number | null;
    warning: string | null;
};
export declare const renderIdenticalCallVeto: (tool: string, priorRuns: number, sameResult: boolean) => string;
export declare const renderFailingVeto: (tool: string, failures: number) => string;
export declare const renderCeilingVeto: (max: number) => string;
export declare const NO_PROGRESS_VETO_REPEAT = "Evolve stopped tool calls for this turn: it is not making progress. Reply to the user now.";
/** Key-sorted JSON, so argument order never splits one call into two. */
export declare function canonicalJson(v: unknown): string;
export declare function callHash(toolName: string, args: unknown): string;
export type NoProgressDecision = {
    kind: "unknown";
} | {
    kind: "allow";
} | {
    kind: "veto";
    first: boolean;
    predicate: NoProgressPredicate;
    reason: string;
};
interface Logger {
    info(m: string): void;
    warn(m: string): void;
}
/** Singleton ``noProgressGate``: fed by TurnObserver (model calls, run end),
 *  ToolCallGate (tool calls) and AppIntegrityMiddleware (results). */
export declare class NoProgressGate {
    private runs;
    private sharedDir;
    private botId;
    private registered;
    private maxModelCalls;
    private logger;
    private counters;
    private lastStatusMs;
    private warnedUnknown;
    configure(opts: {
        sharedDir: string;
        botId: string;
        registered: boolean;
        maxModelCallsPerRun: number | null;
        logger?: Logger;
    }): void;
    private run;
    /** llm_output: one model call in the run (the ceiling's evidence). */
    noteModelCall(runId: string | null | undefined, now?: Date): void;
    /** The before_tool_call decision; counts the call. */
    evaluate(runId: string | null | undefined, sessionId: string | null, toolName: string, args: unknown, now?: Date): NoProgressDecision;
    /** Tool-result middleware: the result and whether it failed ("unknown" = no host signal). */
    recordResult(runId: string | null | undefined, toolName: string, args: unknown, resultText: string, outcome: "ok" | "failed" | "unknown", now?: Date): void;
    /** agent_end: the run's ledger is discarded. */
    endRun(runId: string | null | undefined): void;
    private dir;
    private append;
    /** The health control's input. Throttled; atomic tmp+rename. */
    private writeStatus;
}
/** The gateway's one ledger. */
export declare const noProgressGate: NoProgressGate;
export {};
//# sourceMappingURL=NoProgressGate.d.ts.map