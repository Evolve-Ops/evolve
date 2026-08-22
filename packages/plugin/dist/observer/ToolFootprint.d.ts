/**
 * ToolFootprint — boot-time record of what this gateway actually registered.
 *
 * Spec: docs/spec-evolve-overhead-budget-2026-07-31.md (closes A1's declared
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
export declare const TOOL_FOOTPRINT_SCHEMA_VERSION = 1;
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