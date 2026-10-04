/**
 * Housekeeping runs — OpenClaw's pre-compaction memory flush (and, should a
 * future OC run it as an agent turn, compaction itself) — routed to the cheap
 * rung, narrowed to the tools the flush needs, and tagged in the turns log.
 *
 * Brief: internal/dispatch/…/compaction-and-memory-flush-on-cheap-rung.md
 * Finding: internal/finding-cost-forensics-power-bot-2026-09-04.md §2 — ten
 * minutes after a real reply, with 26 tokens of new input, a turn wrote 194k
 * tokens to cache and emitted 12k of output on the session's POWER model: the
 * "store durable memories now" prompt, with every tool loaded. ~$1.90 real
 * for housekeeping nobody asked for.
 *
 * What OpenClaw 2026.9.2 actually does (verified against the installed dist,
 * not the docs — see the PR body for the file:line trail):
 *
 *   - The flush is `runEmbeddedAgent({ trigger: "memory", … })` on the SAME
 *     session key as the conversation it serves. `before_model_resolve` fires
 *     for it with `ctx.trigger === "memory"`, so the plugin can route it, and
 *     that routing holds under any `compaction.memoryFlush.model` config.
 *   - Its tools are already cut to `read` + an append-only `write` by OC
 *     (MEMORY_FLUSH_ALLOWED_TOOL_NAMES). We ALSO return that allow-list from
 *     `before_prompt_build` so the rule survives an OC release that loosens
 *     it — the intersection can only narrow.
 *   - Compaction's summariser is NOT an agent run: it never fires
 *     `before_model_resolve`, and its model/thinking come from
 *     `agents.defaults.compaction.{model,thinkingLevel}` alone. That half of
 *     the rule is enforced by deploy (evolve_admin.compaction_housekeeping),
 *     not here. `"compaction"` is still recognised below so an OC that starts
 *     running it as an agent turn is routed the same way on day one.
 *   - No hook result carries a thinking level or an output cap. The flush's
 *     thinking follows the session's level re-clamped to the cheap model's
 *     catalog; that is the one part of the brief this surface cannot enforce.
 *
 * Nothing here changes which model answers the USER: a housekeeping run never
 * produces a user-visible reply (OC runs it `silentExpected`), and the early
 * return in before_model_resolve never touches the conversation's session
 * class, tier preference or routing state.
 */

import fs from "node:fs";
import path from "node:path";

export type HousekeepingKind = "memory_flush" | "compaction";

/** The per-bot opt-out key: `network.json` `bots.<id>.cost.levers.<name>`. */
export const HOUSEKEEPING_LEVER = "housekeeping_cheap_rung";

/**
 * The tools a flush may use — the SAME two OC 2026.9.2 allows. `write` is the
 * append-only wrapper OC substitutes on a memory run; `read` lets the flush
 * check what the day's note already holds before appending to it.
 */
export const HOUSEKEEPING_TOOLS_ALLOW: readonly string[] = Object.freeze(["read", "write"]);

/**
 * Map OC's `ctx.trigger` to the housekeeping kind, or null for anything else.
 * Accepts the already-normalised tags too, so a turn record re-read through
 * inferTriggerKind keeps its kind.
 */
export function housekeepingKindForTrigger(trigger: unknown): HousekeepingKind | null {
  const t = typeof trigger === "string" ? trigger.trim().toLowerCase() : "";
  if (t === "memory" || t === "memory_flush") return "memory_flush";
  if (t === "compaction") return "compaction";
  return null;
}

/**
 * Resolve a lever's on/off state from a parsed network.json. Default ON:
 * only an explicit `false` disables (D-CS5 — the lever ships enabled, per-bot
 * opt-out, never opt-in). Bot-level wins over pod-level so one bot can opt out
 * of a pod-wide setting and vice versa.
 */
export function leverEnabled(network: unknown, botId: string, lever: string): boolean {
  const net = (network && typeof network === "object") ? network as Record<string, any> : {};
  const botLever = net?.bots?.[botId]?.cost?.levers?.[lever];
  if (typeof botLever === "boolean") return botLever;
  const podLever = net?.cost?.levers?.[lever];
  if (typeof podLever === "boolean") return podLever;
  return true;
}

const LEVER_CACHE_TTL_MS = 60_000;

/**
 * TTL-cached reader for the housekeeping lever. The flush fires at most once
 * per compaction cycle, so the cache is about not re-reading network.json on
 * the hook path, not about volume. Unreadable network.json ⇒ ON (the default
 * is the product behaviour; the file is only ever an opt-out).
 */
export class HousekeepingLeverReader {
  private _value: boolean | null = null;
  private _checkedAt = 0;

  constructor(
    private readonly sharedDir: string,
    private readonly botId: string,
    private readonly now: () => number = Date.now,
  ) {}

  enabled(): boolean {
    const t = this.now();
    if (this._value !== null && t - this._checkedAt < LEVER_CACHE_TTL_MS) return this._value;
    let value = true;
    try {
      const raw = fs.readFileSync(path.join(this.sharedDir, "network.json"), "utf8");
      value = leverEnabled(JSON.parse(raw), this.botId, HOUSEKEEPING_LEVER);
    } catch {
      /* missing / unreadable — keep the default (on) */
    }
    this._value = value;
    this._checkedAt = t;
    return value;
  }
}

/**
 * Pick the cheap-rung model for a housekeeping run: the `fast` role's first
 * model, else the plugin's classifier model (also cheap-rung by default) so a
 * bot without evolve-tiers.json still gets the saving. Null = no override.
 */
export function resolveHousekeepingModel(
  fastRoleModel: string | null | undefined,
  classifierModel: string | null | undefined,
): { model: string; source: "fast_role" | "classifier_model" } | null {
  if (typeof fastRoleModel === "string" && fastRoleModel.trim()) {
    return { model: fastRoleModel.trim(), source: "fast_role" };
  }
  if (typeof classifierModel === "string" && classifierModel.trim()) {
    return { model: classifierModel.trim(), source: "classifier_model" };
  }
  return null;
}
