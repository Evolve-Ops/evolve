/**
 * ToolFootprint — boot-time record of what this gateway actually registered.
 *
 * Spec: internal/spec-evolve-overhead-budget-2026-07-31.md (closes A1's declared
 * v1 gap: per-tool schema weight). The A3 tier campaign showed the static
 * factory universe and the empirically measured per-tier prefix deltas do
 * not reconcile from the outside — only the gateway knows which tools it
 * registered for THIS bot under THIS tier/config, and what their schemas
 * weigh. Record it once per boot.
 *
 * Output: ``{sharedDir}/{botId}/turns/context-footprint.json`` — the turns
 * subdir, NOT the bot root: ``{shared}/{bot}/`` is evolve-owned 0755 on real
 * pods (the gateway user cannot create files there; verified live, the first
 * flush EACCES'd at the root), while ``turns/`` is created by this gateway
 * and is where the prefix-hash ledger already writes.
 * {
 *   schema_version: 1,
 *   ts, bot_id, tier,
 *   tools: [{name, chars}],          // JSON size of {name, description, parameters}
 *   total_chars, tool_count
 * }
 *
 * Design: wraps ``api.registerTool`` once (transparent pass-through), then
 * ``flush()`` after registration completes writes the file (atomic temp +
 * rename, same-dir as the turns files so the admin reads it with existing
 * ACLs). Never throws into plugin startup. Factories are invoked by OC, not
 * by us — we serialize the TOOL DEFINITION the factory returns by invoking
 * it with an empty ctx at flush time ONLY for factories that look pure;
 * instead of risking side effects, we invoke the factory OC-style lazily:
 * the wrapper records the factory and flush() calls it inside try/catch —
 * every existing factory is a pure closure returning {name, description,
 * parameters, execute}, and a throwing factory records {name: <fn name>,
 * chars: -1} rather than breaking the report.
 */
export declare const TOOL_FOOTPRINT_SCHEMA_VERSION = 2;
interface LoggerLike {
    warn: (msg: string) => void;
    info?: (msg: string) => void;
}
type ToolFactory = (ctx: Record<string, unknown>) => {
    name?: string;
    description?: string;
    parameters?: unknown;
};
export interface ToolFootprintRow {
    name: string;
    chars: number;
}
/** Measure one factory's prompt-riding surface. Pure given a pure factory. */
export declare function measureFactory(factory: ToolFactory): ToolFootprintRow;
export interface ToolProfileWeight {
    /** Tools this profile keeps, with their measured weight. */
    tools: ToolFootprintRow[];
    tool_count: number;
    /**
     * What a session on this profile actually puts on the wire: the kept tools
     * at full weight PLUS the stubs the trimmed ones still ride as.
     *
     * This is deliberately NOT the kept-tool sum. A trimmed tool keeps its name
     * and refuses by name, so it is still registered and still costs chars;
     * counting it as 0 is what made an earlier draft of this block report a
     * background session as cheaper than it is.
     */
    total_chars: number;
    /** The kept tools alone — `total_chars` minus `stub_chars`. */
    kept_chars: number;
    /** Tools this profile trims, and what they weigh UNTRIMMED. */
    trimmed: ToolFootprintRow[];
    trimmed_chars: number;
    /** What those trimmed tools still cost as name-only stubs. Never 0 while `trimmed` is non-empty. */
    stub_chars: number;
    /** The real saving this profile buys: `trimmed_chars - stub_chars`. */
    saved_chars: number;
}
/**
 * Weigh one measured tool set under every declared profile (CE-2a).
 *
 * Read-only arithmetic over rows that were already measured — it invokes no
 * factory a second time. A row whose factory threw (``chars: -1``) counts 0
 * toward every total, the same convention ``total_chars`` already uses.
 */
export declare function profileWeights(tools: ToolFootprintRow[]): Record<string, ToolProfileWeight>;
export declare class ToolFootprint {
    private readonly factories;
    private readonly sharedDir;
    private readonly botId;
    private readonly tier;
    private readonly logger;
    constructor(cfg: {
        sharedDir: string;
        botId: string;
        tier: string;
        logger: LoggerLike;
    });
    /** Wrap an api so every registerTool call is recorded, pass-through. */
    wrap<T extends {
        registerTool: (f: ToolFactory, ...rest: unknown[]) => unknown;
    }>(api: T): T;
    /** Serialize all recorded factories and write the footprint file. */
    flush(): void;
}
export {};
//# sourceMappingURL=ToolFootprint.d.ts.map