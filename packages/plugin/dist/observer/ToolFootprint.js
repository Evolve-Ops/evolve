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
import * as fs from "fs";
import * as path from "path";
export const TOOL_FOOTPRINT_SCHEMA_VERSION = 1;
/** Measure one factory's prompt-riding surface. Pure given a pure factory. */
export function measureFactory(factory) {
    try {
        const def = factory({});
        const payload = JSON.stringify({
            name: def?.name,
            description: def?.description,
            parameters: def?.parameters,
        });
        return { name: def?.name ?? factory.name ?? "unknown", chars: payload.length };
    }
    catch {
        return { name: factory.name || "unknown", chars: -1 };
    }
}
export class ToolFootprint {
    factories = [];
    sharedDir;
    botId;
    tier;
    logger;
    constructor(cfg) {
        this.sharedDir = cfg.sharedDir;
        this.botId = cfg.botId;
        this.tier = cfg.tier;
        this.logger = cfg.logger;
    }
    /** Wrap an api so every registerTool call is recorded, pass-through. */
    wrap(api) {
        const original = api.registerTool.bind(api);
        const self = this;
        // Mutating the api object (vs proxying) keeps every other property/method
        // untouched and identity-stable for the rest of the plugin.
        api.registerTool = function (factory, ...rest) {
            self.factories.push(factory);
            return original(factory, ...rest);
        };
        return api;
    }
    /** Serialize all recorded factories and write the footprint file. */
    flush() {
        try {
            const tools = this.factories.map(measureFactory);
            const total = tools.reduce((sum, t) => sum + Math.max(0, t.chars), 0);
            const record = {
                schema_version: TOOL_FOOTPRINT_SCHEMA_VERSION,
                ts: new Date().toISOString(),
                bot_id: this.botId,
                tier: this.tier,
                tools,
                tool_count: tools.length,
                total_chars: total,
            };
            const dir = path.join(this.sharedDir, this.botId, "turns");
            fs.mkdirSync(dir, { recursive: true });
            const dest = path.join(dir, "context-footprint.json");
            // Unique tmp name per process: the plugin also loads inside `openclaw`
            // CLI processes running as OTHER users; with a fixed ".tmp" name, one
            // such process can create a tmp it cannot rename over the gateway's
            // file (sticky-bit dir), leaving a foreign-owned orphan that EACCES-
            // blocks every later writer at open() — observed live on the primary
            // (evo-account gateway vs an `evolve`-user CLI orphan, 2026-08-01).
            const tmp = `${dest}.${process.pid}.tmp`;
            try {
                // Best-effort sweep of any legacy fixed-name orphan (pre-fix
                // deployments) and our own stale pid-tmps from crashed runs.
                for (const entry of fs.readdirSync(dir)) {
                    if (entry.startsWith("context-footprint.json.") && entry.endsWith(".tmp")) {
                        try {
                            fs.unlinkSync(path.join(dir, entry));
                        }
                        catch { /* foreign-owned — ignore */ }
                    }
                }
            }
            catch { /* sweep is best-effort */ }
            fs.writeFileSync(tmp, JSON.stringify(record, null, 1) + "\n", { mode: 0o644 });
            try {
                fs.renameSync(tmp, dest);
            }
            catch (renameErr) {
                // Sticky-dir rename refusal (dest owned by another uid): don't leave
                // the tmp behind as a poison pill for anyone.
                try {
                    fs.unlinkSync(tmp);
                }
                catch { /* already gone */ }
                throw renameErr;
            }
            this.logger.info?.(`Evolve: context footprint — ${tools.length} tools, ` +
                `${total} chars (~${Math.round(total / 4)} tok) for ${this.botId} (tier=${this.tier})`);
        }
        catch (err) {
            // EACCES (dir owned by another gateway) or anything else — never break boot.
            this.logger.warn(`Evolve: context footprint write failed: ${err}`);
        }
    }
}
//# sourceMappingURL=ToolFootprint.js.map