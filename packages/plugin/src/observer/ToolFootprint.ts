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

import * as fs from "fs";
import * as path from "path";

import {
  FULL_PROFILE_ID,
  PROFILE_BY_KIND,
  TOOL_PROFILES,
  profileAllows,
  stubChars,
} from "../tools/ToolProfiles.js";
import type { SessionKind } from "../tools/ToolProfiles.js";

//: v2 adds the ``profiles`` block (context-economy CE-2a): the same tools
//: weighed under each tool profile, so `context_health --overhead` and the
//: census can say what a background session pays versus a user session
//: WITHOUT waiting for one of each to run. v1 fields are unchanged, so a
//: reader that predates this keeps working.
export const TOOL_FOOTPRINT_SCHEMA_VERSION = 2;

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
export function measureFactory(factory: ToolFactory): ToolFootprintRow {
  try {
    const def = factory({});
    const payload = JSON.stringify({
      name: def?.name,
      description: def?.description,
      parameters: def?.parameters,
    });
    return { name: def?.name ?? factory.name ?? "unknown", chars: payload.length };
  } catch {
    return { name: factory.name || "unknown", chars: -1 };
  }
}

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
 * The session kind whose stub description is WIDEST for a profile.
 *
 * The stub text embeds the session kind, so two kinds sharing a profile
 * (`scheduled` and `oneshot` both map to `no_live_speaker`) differ by a couple
 * of chars per tool. Weighing the longest is the conservative choice: the
 * footprint may overstate a stub by a byte or two, never understate it.
 */
function widestKindFor(profileId: string): SessionKind {
  let widest = "";
  for (const [kind, id] of Object.entries(PROFILE_BY_KIND)) {
    if (id === profileId && kind.length > widest.length) widest = kind;
  }
  return (widest || profileId) as SessionKind;
}

/**
 * Weigh one measured tool set under every declared profile (CE-2a).
 *
 * Read-only arithmetic over rows that were already measured — it invokes no
 * factory a second time. A row whose factory threw (``chars: -1``) counts 0
 * toward every total, the same convention ``total_chars`` already uses.
 */
export function profileWeights(
  tools: ToolFootprintRow[],
): Record<string, ToolProfileWeight> {
  const out: Record<string, ToolProfileWeight> = {};
  for (const [id, profile] of Object.entries(TOOL_PROFILES)) {
    const kept: ToolFootprintRow[] = [];
    const trimmed: ToolFootprintRow[] = [];
    for (const row of tools) {
      (profileAllows(profile, row.name) ? kept : trimmed).push(row);
    }
    const sum = (rows: ToolFootprintRow[]) =>
      rows.reduce((acc, r) => acc + Math.max(0, r.chars), 0);
    const kind = widestKindFor(id);
    // A trimmed tool is still registered, as a stub. Weigh it.
    const stub = trimmed.reduce((acc, r) => acc + stubChars(r.name, profile, kind), 0);
    const keptChars = sum(kept);
    const trimmedChars = sum(trimmed);
    out[id] = {
      tools: kept,
      tool_count: kept.length,
      total_chars: keptChars + stub,
      kept_chars: keptChars,
      trimmed,
      trimmed_chars: trimmedChars,
      stub_chars: stub,
      saved_chars: trimmedChars - stub,
    };
  }
  return out;
}

export class ToolFootprint {
  private readonly factories: ToolFactory[] = [];
  private readonly sharedDir: string;
  private readonly botId: string;
  private readonly tier: string;
  private readonly logger: LoggerLike;

  constructor(cfg: { sharedDir: string; botId: string; tier: string; logger: LoggerLike }) {
    this.sharedDir = cfg.sharedDir;
    this.botId = cfg.botId;
    this.tier = cfg.tier;
    this.logger = cfg.logger;
  }

  /** Wrap an api so every registerTool call is recorded, pass-through. */
  wrap<T extends { registerTool: (f: ToolFactory, ...rest: unknown[]) => unknown }>(api: T): T {
    const original = api.registerTool.bind(api);
    const self = this;
    // Mutating the api object (vs proxying) keeps every other property/method
    // untouched and identity-stable for the rest of the plugin.
    (api as { registerTool: unknown }).registerTool = function (
      factory: ToolFactory, ...rest: unknown[]
    ) {
      self.factories.push(factory);
      return original(factory, ...rest);
    };
    return api;
  }

  /** Serialize all recorded factories and write the footprint file. */
  flush(): void {
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
        profiles: profileWeights(tools),
        kind_profiles: PROFILE_BY_KIND,
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
            try { fs.unlinkSync(path.join(dir, entry)); } catch { /* foreign-owned — ignore */ }
          }
        }
      } catch { /* sweep is best-effort */ }
      fs.writeFileSync(tmp, JSON.stringify(record, null, 1) + "\n", { mode: 0o644 });
      try {
        fs.renameSync(tmp, dest);
      } catch (renameErr) {
        // Sticky-dir rename refusal (dest owned by another uid): don't leave
        // the tmp behind as a poison pill for anyone.
        try { fs.unlinkSync(tmp); } catch { /* already gone */ }
        throw renameErr;
      }
      const trimmed = Object.entries(record.profiles)
        .filter(([id]) => id !== FULL_PROFILE_ID)
        .map(([id, p]) => `${id}=${p.total_chars}`)
        .join(" ");
      this.logger.info?.(
        `Evolve: context footprint — ${tools.length} tools, ` +
        `${total} chars (~${Math.round(total / 4)} tok) for ${this.botId} ` +
        `(tier=${this.tier})` + (trimmed ? `; per-profile chars ${trimmed}` : ""),
      );
    } catch (err) {
      // EACCES (dir owned by another gateway) or anything else — never break boot.
      this.logger.warn(`Evolve: context footprint write failed: ${err}`);
    }
  }
}
