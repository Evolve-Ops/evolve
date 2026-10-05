/**
 * HookFireLedger — per-hour count of `before_model_resolve` fires, INCLUDING
 * fires on Evolve's own subagent sessions and early-returning ones.
 *
 * Early warning for recursion (the 2026-09-08 loop: 461 fires in 3 minutes on
 * a bot whose steady state is a dozen a day). Python reads the file.
 *
 *   {sharedDir}/{botId}/turns/hook-fires-<YYYY-MM-DD>.json   (UTC date)
 *   {"schema_version":1,"bot_id":"…","date":"YYYY-MM-DD",
 *    "hours":{"00":{"count":12,"other":0,"keys":{"<sessionKey>":{"n":10,"prefix":"hello"}}}}}
 *
 * Never throws into the hook; every failure is swallowed.
 */
export declare const HOOK_FIRE_MAX_KEYS_PER_HOUR = 50;
export declare const HOOK_FIRE_PREFIX_CHARS = 80;
export declare const HOOK_FIRE_FLUSH_INTERVAL_MS = 10000;
export interface HookFireLedgerOptions {
    sharedDir: string;
    botId: string;
    /** Injected clock (epoch ms). Default Date.now. */
    now?: () => number;
    /** Injected directory override (defaults to {sharedDir}/{botId}/turns). */
    dir?: string;
    debug?: (msg: string) => void;
    /** Set false in tests to skip the background timer. Default true. */
    autoFlush?: boolean;
}
export declare class HookFireLedger {
    private readonly botId;
    private readonly dir;
    private readonly now;
    private readonly debug;
    private readonly autoFlush;
    private readonly days;
    private readonly loaded;
    /** Dates with unflushed counts — only these are rewritten, so a long-lived
     *  gateway does not rewrite every past day's file every flush. */
    private readonly dirtyDates;
    private timer;
    constructor(opts: HookFireLedgerOptions);
    private fileFor;
    /** Load the day's on-disk file once, so a restart does not zero counts. */
    private hoursFor;
    /** Count one fire. Never throws. */
    record(sessionKey: unknown, userMessage: unknown): void;
    private schedule;
    /** Write every dirty day to disk (atomic tmp+rename). Never throws. */
    flush(): void;
}
//# sourceMappingURL=HookFireLedger.d.ts.map